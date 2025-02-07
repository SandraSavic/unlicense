import functools
import logging
from importlib import resources
from pathlib import Path
from typing import (List, Callable, Dict, Any, Optional)

import frida  # type: ignore
import frida.core  # type: ignore

from .process_control import (ProcessController, Architecture, MemoryRange,
                              QueryProcessMemoryError, ReadProcessMemoryError,
                              WriteProcessMemoryError)

LOG = logging.getLogger(__name__)
# See issue #7: messages cannot exceed 128MiB
MAX_DATA_CHUNK_SIZE = 64 * 1024 * 1024


class FridaProcessController(ProcessController):

    def __init__(self, pid: int, main_module_name: str,
                 frida_session: frida.core.Session,
                 frida_script: frida.core.Script):
        frida_rpc = frida_script.exports

        # Initialize ProcessController
        super().__init__(pid, main_module_name,
                         _str_to_architecture(frida_rpc.get_architecture()),
                         frida_rpc.get_pointer_size(),
                         frida_rpc.get_page_size())

        # Initialize FridaProcessController specifics
        self._frida_rpc = frida_rpc
        self._frida_session = frida_session
        self._exported_functions_cache: Optional[Dict[int, Dict[str,
                                                                Any]]] = None

    def find_module_by_address(self, address: int) -> Optional[Dict[str, Any]]:
        value: Optional[Dict[
            str, Any]] = self._frida_rpc.find_module_by_address(address)
        return value

    def find_range_by_address(
            self,
            address: int,
            include_data: bool = False) -> Optional[MemoryRange]:
        value: Optional[Dict[
            str, Any]] = self._frida_rpc.find_range_by_address(address)
        if value is None:
            return None
        return self._frida_range_to_mem_range(value, include_data)

    def enumerate_modules(self) -> List[str]:
        value: List[str] = self._frida_rpc.enumerate_modules()
        return value

    def enumerate_module_ranges(
            self,
            module_name: str,
            include_data: bool = False) -> List[MemoryRange]:
        convert_range = lambda r: self._frida_range_to_mem_range(
            r, include_data)

        value: List[Dict[str, Any]] = self._frida_rpc.enumerate_module_ranges(
            module_name)
        return list(map(convert_range, value))

    def enumerate_exported_functions(self,
                                     update_cache: bool = False
                                     ) -> Dict[int, Dict[str, Any]]:
        if self._exported_functions_cache is None or update_cache:
            value: List[Dict[
                str, Any]] = self._frida_rpc.enumerate_exported_functions()
            exports_dict = {int(e["address"], 16): e for e in value}
            self._exported_functions_cache = exports_dict
            return exports_dict
        return self._exported_functions_cache

    def allocate_process_memory(self, size: int, near: int) -> int:
        buffer_addr = self._frida_rpc.allocate_process_memory(size, near)
        return int(buffer_addr, 16)

    def query_memory_protection(self, address: int) -> str:
        try:
            protection: str = self._frida_rpc.query_memory_protection(address)
            return protection
        except frida.core.RPCException as e:
            raise QueryProcessMemoryError from e

    def set_memory_protection(self, address: int, size: int,
                              protection: str) -> bool:
        result: bool = self._frida_rpc.set_memory_protection(
            address, size, protection)
        return result

    def read_process_memory(self, address: int, size: int) -> bytes:
        read_data = bytearray()
        try:
            for offset in range(0, size, MAX_DATA_CHUNK_SIZE):
                chunk_size = min(MAX_DATA_CHUNK_SIZE, size - offset)
                read_data += bytearray(
                    self._frida_rpc.read_process_memory(
                        address + offset, chunk_size))
            return bytes(read_data)
        except frida.core.RPCException as e:
            raise ReadProcessMemoryError from e

    def write_process_memory(self, address: int, data: List[int]) -> None:
        try:
            self._frida_rpc.write_process_memory(address, data)
        except frida.core.RPCException as e:
            raise WriteProcessMemoryError from e

    def terminate_process(self) -> None:
        frida.kill(self.pid)
        self._frida_session.detach()

    def _frida_range_to_mem_range(self, r: Dict[str, Any],
                                  with_data: bool) -> MemoryRange:
        base = int(r["base"], 16)
        size = r["size"]
        data = None
        if with_data:
            data = self.read_process_memory(base, size)
        return MemoryRange(base=base,
                           size=size,
                           protection=r["protection"],
                           data=data)


def _str_to_architecture(frida_arch: str) -> Architecture:
    if frida_arch == "ia32":
        return Architecture.X86_32
    if frida_arch == "x64":
        return Architecture.X86_64
    raise ValueError


def spawn_and_instrument(
        exe_path: Path,
        notify_oep_reached: Callable[[int, int], None]) -> ProcessController:
    main_module_name = exe_path.name
    pid: int = frida.spawn((str(exe_path), ))
    session = frida.attach(pid)
    frida_js = resources.open_text("unlicense.resources", "frida.js").read()
    script = session.create_script(frida_js)
    on_message_callback = functools.partial(_frida_callback,
                                            notify_oep_reached)
    script.on('message', on_message_callback)
    script.load()

    frida_rpc = script.exports
    process_controller = FridaProcessController(pid, main_module_name, session,
                                                script)
    frida_rpc.setup_oep_tracing(exe_path.name)
    frida.resume(pid)

    return process_controller


def _frida_callback(notify_oep_reached: Callable[[int, int], None],
                    message: Dict[str, Any], _data: Any) -> None:
    msg_type = message['type']
    if msg_type == 'error':
        LOG.error(message)
        LOG.error(message['stack'])
        return

    if msg_type == 'send':
        payload = message['payload']
        event = payload.get('event', '')
        if event == 'oep_reached':
            # Note: We cannot use RPCs in `on_message` callbacks, so we have to
            # delay the actual dumping.
            notify_oep_reached(int(payload['BASE'], 16),
                               int(payload['OEP'], 16))
            return

    raise NotImplementedError('Unknown message received')

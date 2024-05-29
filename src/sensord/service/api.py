import json
import logging
import os
from abc import ABC, abstractmethod
from json import JSONDecodeError

import sensord.service.sen0395
from sensation.sen0395 import Command
from sensord import common
from sensord.cli.client import APIClient
from sensord.common.sen0395 import SensorStatuses, SensorConfigChainResponse, SensorCommandResponse
from sensord.common.socket import SocketServer, SocketServerStoppedAlready
from sensord.service import paths
from sensord.service.err import ServiceAlreadyRunning

log = logging.getLogger(__name__)

API_FILE_EXTENSION = '.api'


def _create_socket_name():
    return common.unique_timestamp_hex() + API_FILE_EXTENSION


class _ApiError(Exception):

    def __init__(self, code, error):
        self.code = code
        self.error = error

    def create_response(self, id=None):
        return _resp_err(self.code, self.error, id)


def _missing_field_error(field) -> _ApiError:
    return _ApiError(-32602, f"Missing field: {field}")


def _no_sensor_error(sensor) -> _ApiError:
    return _ApiError(-32001, f"Sensor {sensor} not found")


def _no_sensors_error() -> _ApiError:
    return _ApiError(-32002, f"No sensors found")


def _unknown_command_error(cmd) -> _ApiError:
    return _ApiError(-32003, f"Command {cmd} is not recognized")


def _resp_ok(result, id=None):
    return _resp(result, id)


def _resp(result, id=None):
    resp = {
        "jsonrpc": "2.0",
        "result": result,
        "id": id
    }
    return json.dumps(resp)


def _resp_err(code: int, message: str, id=None):
    err_resp = {
        "jsonrpc": "2.0",
        "error": {
            "code": code,
            "message": message
        },
        "id": id
    }

    return json.dumps(err_resp)


class APIMethod(ABC):

    @property
    @abstractmethod
    def method(self):
        """Name of the JSON-RPC method"""

    @abstractmethod
    def handle(self, params):
        """Handle request and optionally return response or raise :class:`__ServerError"""

    def validate(self, params):
        """Raise :class:`__ServerError if params are invalid"""


def _get_sensors(sensor_name):
    if sensor_name:
        sensor = sensord.service.sen0395.get_sensor(sensor_name)
        if not sensor:
            raise _no_sensor_error(sensor_name)

        return [sensor]

    sensors = sensord.service.sen0395.get_all_sensors()

    if not sensors:
        raise _no_sensors_error()

    return sensors


class APISen0395Command(APIMethod):

    @property
    def method(self):
        return 'sen0395.command'

    def handle(self, params):
        sensors = _get_sensors(params.get('name'))

        cmd = Command.from_value(params['command'])

        if not cmd:
            raise _unknown_command_error(params['command'])

        args = params.get('args') or ()

        responses = []
        for sensor in sensors:
            cmd_resp = sensor.send_command(cmd, *args)
            responses.append(SensorCommandResponse(sensor.sensor_id, cmd_resp).serialize())

        return {"sensor_command_responses": responses}

    def validate(self, params):
        if 'command' not in params:
            raise _missing_field_error('command')


class APISen0395Configure(APIMethod):

    @property
    def method(self):
        return 'sen0395.configure'

    def handle(self, params):
        sensors = _get_sensors(params.get('name'))

        cmd = Command.from_value(params['command'])

        if not cmd:
            raise _unknown_command_error(params['command'])

        if not cmd.is_config:
            raise _ApiError(-32004, f"Command {cmd} is not a configuration command")

        args = params.get('args') or ()

        responses = []
        for sensor in sensors:
            config_chain_resp = sensor.configure(cmd, *args)
            responses.append(SensorConfigChainResponse(sensor.sensor_id, config_chain_resp).serialize())

        return {"sensor_config_chain_responses": responses}

    def validate(self, params):
        if 'command' not in params:
            raise _missing_field_error('command')


class APISen0395Status(APIMethod):

    @property
    def method(self):
        return 'sen0395.status'

    def handle(self, params):
        sensors = _get_sensors(params.get('name'))

        statuses = []
        for sensor in sensors:
            statuses.append(sensor.status())

        return SensorStatuses(statuses).serialize()


class APISen0395Reading(APIMethod):

    @property
    def method(self):
        return 'sen0395.reading'

    def handle(self, params):
        sensors = _get_sensors(params.get('name'))

        if 'enabled' not in params:
            raise _missing_field_error('enabled')

        enabled = params['enabled']
        statuses = []
        for sensor in sensors:
            if enabled:
                sensor.clear_buffer()
                sensor.start_reading()
            else:
                sensor.stop_reading()

            statuses.append(sensor.status())

        return SensorStatuses(statuses).serialize()


DEFAULT_METHODS = (APISen0395Command(), APISen0395Configure(), APISen0395Status(), APISen0395Reading())


class APIServer(SocketServer):

    def __init__(self, socket_path, methods=DEFAULT_METHODS):
        super().__init__(socket_path, allow_ping=True)  # Allow ping for stale socket check
        self._methods = {method.method: method for method in methods}

    def handle(self, req):
        try:
            req_body = json.loads(req)
        except JSONDecodeError as e:
            log.warning(f"event=[invalid_json_request_body] length=[{e}]")
            return _resp_err(-32700, "Parse error")

        if 'jsonrpc' not in req_body or req_body['jsonrpc'] != '2.0':
            return _resp_err(-32600, "Invalid Request")

        if 'method' not in req_body:
            return _resp_err(-32600, "Invalid Request")

        method_name = req_body['method']
        params = req_body.get('params', {})
        request_id = req_body.get('id')

        try:
            method = self._resolve_method(method_name)
            method.validate(params)
        except _ApiError as e:
            return e.create_response(request_id)

        try:
            result = method.handle(params)
            return _resp_ok(result, request_id)
        except _ApiError as e:
            return e.create_response(request_id)
        except Exception:
            log.error("event=[api_handler_error]", exc_info=True)
            return _resp_err(-32603, "Internal error", request_id)

    def _resolve_method(self, method_name) -> APIMethod:
        method = self._methods.get(method_name)
        if not method:
            raise _ApiError(-32601, f"Method not found: {method_name}")

        return method


_api_server = APIServer(paths.api_socket_path())


def start():
    socket_path = paths.api_socket_path()

    with APIClient(socket_path) as client:
        ping_result = client.ping()

    if ping_result.active_servers or ping_result.timed_out_servers:
        raise ServiceAlreadyRunning

    if ping_result.stale_sockets and os.path.exists(socket_path):
        os.remove(socket_path)
        log.warning("[stale_socket_removed]")

    try:
        _api_server.start()
    except SocketServerStoppedAlready:
        pass  # Stopped before started -> ignore..


def stop():
    _api_server.close_and_wait()

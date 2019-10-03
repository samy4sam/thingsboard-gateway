import logging
import time
import yaml
from json import load, loads, dumps

from gateway.tb_client import TBClient
from tb_utility.tb_utility import TBUtility
from threading import Thread
from connectors.mqtt.mqtt_connector import MqttConnector
from storage.memory_event_storage import MemoryEventStorage
from storage.file_event_storage import FileEventStorage


log = logging.getLogger('__main__')
log.setLevel(logging.DEBUG)


class TBGatewayService:
    def __init__(self, config_file):
        with open(config_file) as config:
            config = yaml.safe_load(config)
            self.available_connectors = {}
            # TODO: add persistance of the __connected_devices dictionary
            self.__connected_devices = {}
            self.__connector_incoming_messages = {}
            self.__send_thread = Thread(target=self.__read_data_from_storage, daemon=True)
            if config["storage"]["type"] == "memory":
                self.__event_storage = MemoryEventStorage(config["storage"])
            else:
                self.__event_storage = FileEventStorage(config["storage"])
            self.__events = []
            self.tb_client = TBClient(config["thingsboard-client"])
            self.tb_client._client.gw_set_server_side_rpc_request_handler(self.__rpc_request_handler)
            self.tb_client.connect()
            self.__rpc_requests_in_progress = {}
            self.__load_connectors(config)
            self.__connect_with_connectors()
            self.tb_client._client.gw_subscribe_to_all_attributes(self.__attribute_update_callback)
            self.__send_thread.start()

            while True:
                for rpc_in_progress in self.__rpc_requests_in_progress:
                    if time.time() >= self.__rpc_requests_in_progress[rpc_in_progress][1]:
                        self.__rpc_requests_in_progress[rpc_in_progress][2](rpc_in_progress)
                        del self.__rpc_requests_in_progress[rpc_in_progress]
                time.sleep(.1)

    def __load_connectors(self, config):
        self._connectors_configs = {}
        for connector in config['connectors']:
            try:
                with open('config/'+connector['configuration'], 'r') as conf_file:
                    connector_conf = load(conf_file)
                    if not self._connectors_configs.get(connector['type']):
                        self._connectors_configs[connector['type']] = []
                    self._connectors_configs[connector['type']].append({connector['configuration']: connector_conf})
            except Exception as e:
                log.error(e)

    def __connect_with_connectors(self):
        for connector_type in self._connectors_configs:
            if connector_type == "mqtt":
                for connector_config in self._connectors_configs[connector_type]:
                    for config_file in connector_config:
                        try:
                            connector = MqttConnector(self, connector_config[config_file])
                            self.available_connectors[connector.getName()] = connector
                            connector.open()
                        except Exception as e:
                            log.error(e)

    def _send_to_storage(self, connector_name, data):
        if not TBUtility.validate_converted_data(data):
            log.error("Data from %s connector is invalid.", connector_name)
            return
        if data["deviceName"] not in self.__connected_devices:
            self.__connected_devices[data["deviceName"]]["connector"] = self.available_connectors[connector_name]
            self.tb_client._client.gw_connect_device(data["deviceName"]).wait_for_publish()
        if not self.__connector_incoming_messages.get(connector_name):
            self.__connector_incoming_messages[connector_name] = 0
        else:
            self.__connector_incoming_messages[connector_name] += 1
        json_data = dumps(data)
        save_result = self.__event_storage.put(json_data)
        if save_result:
            log.debug('Connector "%s" - Saved information - %s', connector_name, json_data)
        else:
            log.error('Data from connector "%s" cannot be saved.')

    def __read_data_from_storage(self):
        while True:
            try:
                self.__published_events = []
                events = self.__event_storage.get_event_pack()
                if events:
                    for event in events:
                        current_event = loads(event)
                        if current_event["deviceName"] not in self.__connected_devices:
                            self.tb_client._client.gw_connect_device(current_event["deviceName"]).wait_for_publish()
                        self.__connected_devices[current_event["deviceName"]]["current_event"] = current_event["deviceName"]
                        if current_event.get("telemetry"):
                            data_to_send = loads('{"ts": %i,"values": %s}'%(time.time(), ','.join(dumps(param) for param in current_event["telemetry"])))
                            self.__published_events.append(self.tb_client._client.gw_send_telemetry(current_event["deviceName"], data_to_send))
                        if current_event.get("attributes"):
                            data_to_send = loads('%s' % (','.join(dumps(param) for param in current_event["attributes"])))
                            self.__published_events.append(self.tb_client._client.gw_send_attributes(current_event["deviceName"], data_to_send))
                    success = True
                    for event in range(len(self.__published_events)):
                        result = self.__published_events[event].get()
                        success = result == self.__published_events[event].TB_ERR_SUCCESS
                    if success:
                        self.__event_storage.event_pack_processing_done()
                else:
                    time.sleep(1)
            except Exception as e:
                log.error(e)
                time.sleep(10)

    def __rpc_request_handler(self, _, content):
        device = content.get("device")
        if device is not None:
            connector = self.__connected_devices[device].get("connector")
            if connector is not None:
                connector.server_side_rpc_handler(content)
            else:
                log.error("Received RPC request but connector for device %s not found. Request data: \n %s",
                          content["device"],
                          dumps(content))
        else:
            log.debug("RPC request with no device param.")

    def rpc_with_reply_processing(self, topic, content):
        req_id = self.__rpc_requests_in_progress[topic][0]["data"]["id"]
        device = self.__rpc_requests_in_progress[topic][0]["device"]
        self.tb_client._client.gw_send_rpc_reply(device, req_id, content)
        self.cancel_rpc_request(topic)

    def register_rpc_request_timeout(self, content, timeout, topic, cancel_method):
        self.__rpc_requests_in_progress[topic] = (content, timeout, cancel_method)

    def cancel_rpc_request(self, rpc_request):
        del self.__rpc_requests_in_progress[rpc_request]

    def __attribute_update_callback(self, content):
        self.__connected_devices[content["device"]]["connector"].on_attributes_update(content)

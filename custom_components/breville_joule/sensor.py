"""Sensor platform for Breville Joule integration."""

import asyncio
import json
import logging
import threading
import time
import urllib.parse
import jwt

from websocket import WebSocketApp
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers import aiohttp_client

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class BrevilleAuth:
    """Provide Breville authentication for simple username/password auth."""

    def __init__(
        self, hass: HomeAssistant, username: str, password: str, polling_interval: int
    ):
        self.hass = hass
        self.username = username
        self.password = password
        self.polling_interval = polling_interval
        self.appliances = []
        self._access_token = None
        self._user_id = None
        self._ws = None
        self._ws_thread = None

    async def async_get_access_token(self) -> str:
        """Fetch and return the access token."""
        if not self._access_token:
            auth_url = "https://my.breville.com/oauth/token"
            auth_payload = {
                "username": self.username,
                "password": self.password,
                "realm": "Salesforce",
                "scope": "openid profile email offline_access",
                "audience": "https://iden-prod.us.auth0.com/userinfo",
                "client_id": "A2IYXGeuX1g8s049YEri6WC6hu2wlrMZ",
                "grant_type": "http://auth0.com/oauth/grant-type/password-realm",
            }
            auth_headers = {
                "accept": "application/json",
                "content-type": "application/json",
                "priority": "u=3, i",
                "user-agent": "Breville/827 CFNetwork/1568.200.51 Darwin/24.1.0",
            }

            session = aiohttp_client.async_get_clientsession(self.hass)
            async with session.post(auth_url, json=auth_payload, headers=auth_headers) as resp:
                resp.raise_for_status()
                token_data = await resp.json()
                self._access_token = token_data["id_token"]
                self._user_id = jwt.decode(
                    self._access_token,
                    options={"verify_signature": False},
                    algorithms=["RS256"],
                )["sub"]

        return self._access_token

    async def async_connect_websocket(self):
        """Establish WebSocket connection using the access token."""
        try:
            access_token = await self.async_get_access_token()
            self._ws = WebSocketApp(
                "wss://iot-api-ws.breville.com/applianceProxy",
                header={"sf-id-token": access_token},
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            self._ws.on_open = self._on_open
            self._ws_thread = threading.Thread(target=self._ws.run_forever, daemon=True)
            self._ws_thread.start()
            return self._ws
        except Exception as e:
            _LOGGER.error("Error while connecting WebSocket: %s", e)
            self._ws = None
            raise

    def _on_message(self, ws, message):
        try:
            asyncio.run_coroutine_threadsafe(
                self._handle_websocket_message(message), self.hass.loop
            )
        except json.JSONDecodeError as e:
            _LOGGER.error("Failed to decode message: %s", e)

    def _on_error(self, ws, error):
        _LOGGER.error("WebSocket error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg):
        _LOGGER.warning("WebSocket closed: %s %s", close_status_code, close_msg)

    def _on_open(self, ws):
        asyncio.run_coroutine_threadsafe(
            self._async_send_appliances(ws), self.hass.loop
        )

    async def _async_send_appliances(self, ws):
        """Send appliance info when WebSocket opens."""
        self.appliances.clear()
        session = aiohttp_client.async_get_clientsession(self.hass)
        async with session.get(
            f"https://iot-api.breville.com/user/v2/user/{urllib.parse.quote(self._user_id)}/appliances",
            headers={"sf-id-token": await self.async_get_access_token()},
        ) as resp:
            if resp.status == 200:
                appliances_data = await resp.json()
                for appliance in appliances_data.get("appliances", []):
                    self.appliances.append(appliance)
                    if appliance["model"] == "BSV600":
                        add_appliance = {
                            "action": "addAppliance",
                            "serialNumber": appliance["serialNumber"],
                        }
                        ws.send(json.dumps(add_appliance))

        asyncio.create_task(self._async_ping(ws))
        asyncio.create_task(self._async_poll(ws))

    async def _async_ping(self, ws):
        while True:
            await asyncio.sleep(20)
            if ws.sock and ws.sock.connected:
                ws.send(json.dumps({"action": "ping"}))
            else:
                break

    async def _async_poll(self, ws):
        while True:
            try:
                await asyncio.sleep(self.polling_interval)
                if ws.sock and ws.sock.connected:
                    for appliance in self.appliances:
                        add_appliance = {
                            "action": "addAppliance",
                            "serialNumber": appliance["serialNumber"],
                        }
                        ws.send(json.dumps(add_appliance))
                else:
                    break
            except Exception as e:
                _LOGGER.error("Error handling SyncPoll message: %s", e)

    async def _handle_websocket_message(self, message):
        try:
            data = json.loads(message)
            if data.get("messageType") == "stateReport":
                reported = data.get("data", {}).get("reported", {})
                if isinstance(reported, dict):
                    start_time, end_time = None, None
                    setpoint = None
                    has_timer = False
                    if "timers" in reported and isinstance(reported["timers"], list):
                        timer = reported["timers"][0]
                        if "timestamp" in timer:
                            has_timer = True
                            start_time = time.strftime(
                                "%Y-%m-%d %H:%M:%S",
                                time.localtime(timer.get("timestamp", 0)),
                            )
                        if "down_from_n" in timer:
                            end_time = time.strftime(
                                "%Y-%m-%d %H:%M:%S",
                                time.localtime(
                                    timer.get("timestamp", 0) + timer.get("down_from_n", 0)
                                ),
                            )

                    for entity in self.hass.data.get(DOMAIN, {}).get("entities", []):
                        if isinstance(entity, BrevilleJouleStartTimeSensor):
                            entity.update_state(start_time)
                        elif isinstance(entity, BrevilleJouleEndTimeSensor):
                            entity.update_state(end_time)
                        elif isinstance(entity, BrevilleJouleStateSensor):
                            entity.update_state(has_timer)
                        elif isinstance(entity, BrevilleJouleSensor):
                            if "heaters" in reported and reported["heaters"]:
                                cur_temp = reported["heaters"][0].get("cur_temp", "N/A")
                                entity.update_state(cur_temp)
                        elif isinstance(entity, BrevilleJouleSetpointSensor):
                            if "heaters" in reported and reported["heaters"]:
                                setpoint = reported["heaters"][0].get("temp_sp", "N/A")
                                entity.update_state(setpoint)

        except Exception as e:
            _LOGGER.error("Error handling WebSocket message: %s", e)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Breville Joule sensor platform."""
    auth = hass.data[DOMAIN]["auth"]
    await auth.async_connect_websocket()

    async_add_entities(
        [
            BrevilleJouleSensor(),
            BrevilleJouleStartTimeSensor(),
            BrevilleJouleEndTimeSensor(),
            BrevilleJouleSetpointSensor(),
            BrevilleJouleStateSensor(),
        ]
    )

# The sensor entity classes remain unchanged from your previous version, as they are structured well.
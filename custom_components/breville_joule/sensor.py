"""Sensor platform for Breville Joule integration."""

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from websocket import WebSocketApp
import asyncio
import json
import threading
import time
import jwt
import urllib.parse
from homeassistant.helpers import aiohttp_client

from .const import DOMAIN


class BrevilleAuth:
    """Provide Breville authentication for simple username/password auth."""

    def __init__(
        self, hass: HomeAssistant, username: str, password: str, polling_interval: int
    ):
        """Initialize Breville Auth."""
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
            async with session.post(
                auth_url, json=auth_payload, headers=auth_headers
            ) as resp:
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

            self._ws_thread = threading.Thread(target=self._ws.run_forever)
            self._ws_thread.daemon = True
            self._ws_thread.start()

            return self._ws
        except Exception as e:
            print(f"Error while connecting WebSocket: {e}")
            self._ws = None
            raise

    def _on_message(self, ws, message):
        """Handle incoming WebSocket messages."""
        try:
            asyncio.run_coroutine_threadsafe(
                self._handle_websocket_message(message), self.hass.loop
            )
        except json.JSONDecodeError as e:
            print(f"Failed to decode message: {e}")

    def _on_error(self, ws, error):
        """Handle WebSocket error."""
        print(f"WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket close."""
        print("WebSocket closed")

    def _on_open(self, ws):
        """Handle WebSocket open event."""
        asyncio.run_coroutine_threadsafe(
            self._async_send_appliances(ws), self.hass.loop
        )

    async def _async_send_appliances(self, ws):
        """Send appliance information when the WebSocket opens."""
        session = aiohttp_client.async_get_clientsession(self.hass)
        async with session.get(
            f"https://iot-api.breville.com/user/v2/user/{urllib.parse.quote(self._user_id)}/appliances",
            headers={"sf-id-token": await self.async_get_access_token()},
        ) as resp:
            if resp.status == 200:
                appliances_data = await resp.json()
                appliances = appliances_data.get("appliances", [])
                for appliance in appliances:
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
        """Send ping messages to keep the WebSocket connection alive."""
        while True:
            await asyncio.sleep(20)
            if ws.sock and ws.sock.connected:
                ws.send(json.dumps({"action": "ping"}))
            else:
                break

    async def _async_poll(self, ws):
        """Send ping messages to keep the WebSocket connection alive."""
        while True:
            try:
                await asyncio.sleep(self.polling_interval)
                if ws.sock and ws.sock.connected:
                    for appliance in self.appliances:
                        # remove_appliance = {
                        #    "action": "removeAppliance",
                        #    "serialNumber": appliance["serialNumber"],
                        # }
                        # ws.send(json.dumps(remove_appliance))

                        # await asyncio.sleep(0.5)

                        add_appliance = {
                            "action": "addAppliance",
                            "serialNumber": appliance["serialNumber"],
                        }
                        ws.send(json.dumps(add_appliance))
                else:
                    break
            except Exception as e:
                print(f"Error handling SyncPoll message: {e}")

    async def _handle_websocket_message(self, message):
        """Handle incoming WebSocket messages."""
        try:
            data = json.loads(message)
            if data.get("messageType") == "stateReport":
                reported = data.get("data", {}).get("reported", {})
                if isinstance(reported, dict):
                    start_time, end_time = None, None
                    setpoint = None
                    has_timer = False
                    if (
                        "timers" in reported
                        and isinstance(reported["timers"], list)
                        and len(reported["timers"]) > 0
                    ):
                        timer = reported["timers"][0]
                        if "timestamp" in timer:
                            has_timer = True
                            start_time = time.strftime(
                                "%Y-%m-%d %H:%M:%S",
                                time.localtime(timer.get("timestamp", 0)),
                            )
                            for entity in self.hass.data.get(DOMAIN, {}).get(
                                "entities", []
                            ):
                                if isinstance(entity, BrevilleJouleStartTimeSensor):
                                    entity.update_state(end_time)
                        if "down_from_n" in timer:
                            end_time = time.strftime(
                                "%Y-%m-%d %H:%M:%S",
                                time.localtime(
                                    timer.get("timestamp", 0)
                                    + timer.get("down_from_n", 0)
                                ),
                            )
                            for entity in self.hass.data.get(DOMAIN, {}).get(
                                "entities", []
                            ):
                                if isinstance(entity, BrevilleJouleEndTimeSensor):
                                    entity.update_state(end_time)
                        for entity in self.hass.data.get(DOMAIN, {}).get(
                            "entities", []
                        ):
                            if isinstance(entity, BrevilleJouleStateSensor):
                                entity.update_state(has_timer)

                    if (
                        "heaters" in reported
                        and isinstance(reported["heaters"], list)
                        and reported["heaters"]
                    ):
                        heater = reported["heaters"][0]
                        setpoint = heater.get("temp_sp", "N/A")
                        cur_temp = heater.get("cur_temp", "N/A")

                        for entity in self.hass.data.get(DOMAIN, {}).get(
                            "entities", []
                        ):
                            if isinstance(entity, BrevilleJouleSensor):
                                entity.update_state(cur_temp)
                            elif isinstance(entity, BrevilleJouleSetpointSensor):
                                entity.update_state(setpoint)

        except Exception as e:
            print(f"Error handling WebSocket message: {e}")


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


class BrevilleJouleSensor(SensorEntity):
    """Representation of a Breville Joule temperature sensor."""

    def __init__(self):
        """Initialize the sensor."""
        self._temperature = None

    @property
    def name(self):
        """Return the name of the sensor."""
        return "Breville Joule"

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._temperature

    @property
    def device_class(self):
        """Return the class of this sensor."""
        return SensorDeviceClass.TEMPERATURE

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return "°C"

    @property
    def force_update(self):
        return True

    def update_state(self, temperature, start_time=None, end_time=None):
        """Update the sensor state."""
        self._temperature = temperature
        self._start_time = start_time
        self._end_time = end_time
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        """Run when entity about to be added to hass."""
        if "entities" not in self.hass.data.setdefault(DOMAIN, {}):
            self.hass.data[DOMAIN]["entities"] = []
        self.hass.data[DOMAIN]["entities"].append(self)


class BrevilleJouleStartTimeSensor(SensorEntity):
    """Representation of a Breville Joule start time sensor."""

    def __init__(self):
        self._start_time = None

    @property
    def name(self):
        return "Breville Joule Start Time"

    @property
    def state(self):
        return self._start_time

    @property
    def device_class(self):
        return SensorDeviceClass.TIMESTAMP

    @property
    def force_update(self):
        return True

    def update_state(self, start_time=None):
        self._start_time = start_time
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        if "entities" not in self.hass.data.setdefault(DOMAIN, {}):
            self.hass.data[DOMAIN]["entities"] = []
        self.hass.data[DOMAIN]["entities"].append(self)


class BrevilleJouleEndTimeSensor(SensorEntity):
    """Representation of a Breville Joule end time sensor."""

    def __init__(self):
        self._end_time = None

    @property
    def name(self):
        return "Breville Joule End Time"

    @property
    def state(self):
        return self._end_time

    @property
    def device_class(self):
        return SensorDeviceClass.TIMESTAMP

    @property
    def force_update(self):
        return True

    def update_state(self, end_time=None):
        self._end_time = end_time
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        if "entities" not in self.hass.data.setdefault(DOMAIN, {}):
            self.hass.data[DOMAIN]["entities"] = []
        self.hass.data[DOMAIN]["entities"].append(self)


class BrevilleJouleSetpointSensor(SensorEntity):
    """Representation of a Breville Joule setpoint sensor."""

    def __init__(self):
        self._setpoint = None

    @property
    def name(self):
        return "Breville Joule Setpoint"

    @property
    def state(self):
        return self._setpoint

    @property
    def device_class(self):
        return SensorDeviceClass.TEMPERATURE

    @property
    def unit_of_measurement(self):
        return "°C"

    @property
    def force_update(self):
        return True

    def update_state(self, setpoint=None):
        self._setpoint = setpoint
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        if "entities" not in self.hass.data.setdefault(DOMAIN, {}):
            self.hass.data[DOMAIN]["entities"] = []
        self.hass.data[DOMAIN]["entities"].append(self)


class BrevilleJouleStateSensor(SensorEntity):
    """Representation of a Breville Joule state sensor."""

    def __init__(self):
        self._state = "idle"

    @property
    def name(self):
        return "Breville Joule State"

    @property
    def state(self):
        return self._state

    @property
    def force_update(self):
        return True

    def update_state(self, has_timer=False):
        self._state = "active" if has_timer else "idle"
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        if "entities" not in self.hass.data.setdefault(DOMAIN, {}):
            self.hass.data[DOMAIN]["entities"] = []
        self.hass.data[DOMAIN]["entities"].append(self)

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import logging
from typing import Optional, Dict, Any
import aiohttp
from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
    SensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.const import CURRENCY_EURO, UnitOfEnergy
from homeassistant.util import dt
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics

from . import DOMAIN
from .auth import get_access_token
from homeassistant.helpers.entity import DeviceInfo

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(minutes=30)  # Update more frequently for better graphs

class PowerPriceData:
    """Stores power price data for forecasting."""
    def __init__(self, local_tz):
        self.prices = {}  # Dictionary of datetime -> price (UTC)
        self._min_price = None
        self._max_price = None
        self._avg_price = None
        self.current_price = None
        self.next_price = None
        self.lowest_price_time = None
        self.highest_price_time = None
        self.local_tz = local_tz
        self.consumption_data = None  # Store consumption data

    def add_entry(self, timestamp: datetime, price: float) -> None:
        """Add a price entry and update statistics."""
        # Ensure timestamp is in UTC
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=ZoneInfo("UTC"))
        else:
            timestamp = timestamp.astimezone(ZoneInfo("UTC"))
        
        # Round to the start of the hour
        clean_timestamp = timestamp.replace(minute=0, second=0, microsecond=0)
        self.prices[clean_timestamp] = price
        self._update_statistics()

    def _update_statistics(self) -> None:
        """Update price statistics."""
        if not self.prices:
            return

        now = datetime.now(ZoneInfo("UTC")).replace(minute=0, second=0, microsecond=0)
        
        # Calculate statistics for all prices
        prices = list(self.prices.values())
        self._min_price = min(prices)
        self._max_price = max(prices)
        self._avg_price = sum(prices) / len(prices)
        
        # Get current and next price
        self.current_price = self.prices.get(now)
        next_hour = now + timedelta(hours=1)
        self.next_price = self.prices.get(next_hour)
        
        # Find times for min/max prices, ensuring we keep timezone info
        self.lowest_price_time = None
        self.highest_price_time = None
        for time, price in self.prices.items():
            if price == self._min_price and (self.lowest_price_time is None or time < self.lowest_price_time):
                self.lowest_price_time = time 
            if price == self._max_price and (self.highest_price_time is None or time < self.highest_price_time):
                self.highest_price_time = time 

        # change highest and lowest price time to local timezone
        self.lowest_price_time = self.lowest_price_time.astimezone(self.local_tz)
        self.highest_price_time = self.highest_price_time.astimezone(self.local_tz)

    def get_current_price(self) -> Optional[float]:
        """Get the current hour's price."""
        now = datetime.now(ZoneInfo("UTC")).replace(minute=0, second=0, microsecond=0)
        return self.prices.get(now)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Ostrom sensor platform."""
    coordinator = OstromDataCoordinator(hass, entry)
    
    # Create entities first
    entities = [
        OstromForecastSensor(coordinator, entry),
        OstromAveragePriceSensor(coordinator, entry),
        OstromMinPriceSensor(coordinator, entry),
        OstromMaxPriceSensor(coordinator, entry),
        OstromNextPriceSensor(coordinator, entry),
        OstromLowestPriceTimeSensor(coordinator, entry),
        OstromHighestPriceTimeSensor(coordinator, entry),
        OstromConsumptionSensor(coordinator, entry),
    ]
    
    # Add entities before the first refresh
    async_add_entities(entities)
    
    # Schedule the first refresh instead of waiting for it
    await coordinator.async_refresh()

class OstromDataCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch Ostrom price data."""
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Initialize coordinator."""
        # Calculate time until next hour
        now = dt.now()
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        # Set initial update interval to time until next hour
        initial_update_interval = (next_hour - now).total_seconds()

        super().__init__(
            hass,
            _LOGGER,
            name="Ostrom Price Data",
            update_interval=timedelta(seconds=initial_update_interval),
        )
        self.client_id = entry.data["client_id"]
        self.client_secret = entry.data["client_secret"]
        self.zip_code = entry.data["zip_code"]
        self.environment = entry.data["environment"]
        self._access_token = None
        self._token_expiration = None
        self._env_prefix = "sandbox.ostrom-api.io" if self.environment == "sandbox" else "production.ostrom-api.io"
        self.local_tz = ZoneInfo(hass.config.time_zone)
        self.contract_id = entry.data.get("contract_id")  # Store contract ID for consumption API
        
        # Add device info
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Ostrom Energy",
            manufacturer="Ostrom API",
            model="Price Monitoring",
            sw_version="0.8",
        )

    async def _async_update_data(self) -> PowerPriceData:
        """Fetch and process price data with proper error handling."""
        try:
            _LOGGER.debug("Starting data update for Ostrom integration")
            
            # After each update, set the update interval to 1 hour
            self.update_interval = timedelta(hours=1)
            
            if not self._access_token or datetime.now(ZoneInfo("UTC")) >= self._token_expiration:
                _LOGGER.info("Access token expired or missing, requesting new token")
                await self._get_access_token()

            raw_data = await self._fetch_prices()
            processed_data = self._process_price_data(raw_data)
            
            # Fetch consumption data - get contract_id if not available
            if not self.contract_id:
                try:
                    _LOGGER.info("No contract ID found, attempting to fetch from API")
                    self.contract_id = await self._fetch_contracts()
                    if self.contract_id:
                        _LOGGER.info("Found electricity contract ID: %s", self.contract_id)
                    else:
                        _LOGGER.warning("No active electricity contract found")
                except Exception as err:
                    _LOGGER.warning("Failed to fetch contract ID: %s", err)
            
            if self.contract_id:
                try:
                    consumption_data = await self._fetch_consumption()
                    processed_data.consumption_data = consumption_data
                    
                    # Process historical consumption data
                    if consumption_data:
                        await self._process_historical_consumption(consumption_data)
                        
                except Exception as err:
                    _LOGGER.warning("Failed to fetch consumption data: %s", err)
                    processed_data.consumption_data = None
            else:
                processed_data.consumption_data = None
            
            _LOGGER.debug("Successfully updated Ostrom price data")
            return processed_data
        except Exception as err:
            _LOGGER.error("Error updating Ostrom price data: %s", err, exc_info=True)
            raise UpdateFailed(f"Error fetching data: {err}") from err

    def _process_price_data(self, prices) -> PowerPriceData:
        """Process the raw price data into PowerPriceData structure."""
        price_data = PowerPriceData(self.local_tz)

        for price_entry in prices:
            try:
                # Parse timestamp and ensure UTC timezone
                timestamp = datetime.fromisoformat(price_entry["date"].replace('Z', '+00:00'))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=ZoneInfo("UTC"))
                else:
                    timestamp = timestamp.astimezone(ZoneInfo("UTC"))

                total_price = (price_entry["grossKwhPrice"] + price_entry["grossKwhTaxAndLevies"]) / 100
                
                price_data.add_entry(
                    timestamp=timestamp,
                    price=round(total_price, 4)
                )
            except Exception as e:
                _LOGGER.error("Error processing price entry: %s", e)

        return price_data

    async def _get_access_token(self):
        """Get access token."""
        try:
            token_data = await get_access_token(self.client_id, self.client_secret, self.environment)
            self._access_token = token_data["access_token"]
            expires_in = token_data.get("expires_in", 3600)
            self._token_expiration = datetime.now(ZoneInfo("UTC")) + timedelta(seconds=expires_in)
        except Exception as e:
            _LOGGER.error("Failed to get access token: %s", str(e))
            raise

    async def _fetch_prices(self):
        """Fetch price data including future prices."""
        now = datetime.now(ZoneInfo("UTC")).replace(minute=0, second=0, microsecond=0)
        url = f"https://{self._env_prefix}/spot-prices"
        
        headers = {"Authorization": f"Bearer {self._access_token}"}
        params = {
            "startDate": (now).strftime("%Y-%m-%dT%H:00:00.000Z"),  
            "endDate": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:00:00.000Z"),
            "resolution": "HOUR",
            "zip": self.zip_code
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                return data["data"]

    async def _fetch_contracts(self):
        """Fetch available contracts and get the first electricity contract ID."""
        url = f"https://{self._env_prefix}/contracts"
        headers = {"Authorization": f"Bearer {self._access_token}"}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                data = await response.json()
                
                # Find the first active electricity contract
                for contract in data.get("data", []):
                    if (contract.get("type") == "ELECTRICITY" and 
                        contract.get("status") == "ACTIVE"):
                        return contract.get("id")
                
                return None

    async def _fetch_consumption(self):
        """Fetch energy consumption data for the previous day."""
        if not self.contract_id:
            _LOGGER.warning("No contract ID available for consumption data")
            return None
            
        # Get previous day's date range in UTC (full 24 hours)
        now = datetime.now(ZoneInfo("UTC"))
        yesterday = now - timedelta(days=1)
        start_date = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        # End date should be start of next day to ensure we get all 24 hours
        end_date = start_date + timedelta(days=1)
        
        url = f"https://{self._env_prefix}/contracts/{self.contract_id}/energy-consumption"
        headers = {"Authorization": f"Bearer {self._access_token}"}
        params = {
            "startDate": start_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "endDate": end_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "resolution": "HOUR"
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                return data

    async def _process_historical_consumption(self, consumption_data):
        """Process historical consumption data and add to recorder statistics."""
        if not isinstance(consumption_data, dict) or "data" not in consumption_data:
            return
            
        hourly_data = consumption_data["data"]
        if not isinstance(hourly_data, list) or len(hourly_data) == 0:
            return
            
        # Create statistics for the consumption sensor
        statistic_id = f"{DOMAIN}:ostrom_consumption_{self.zip_code}"
        
        # Prepare statistics data
        statistics = []
        running_total = 0
        
        for entry in hourly_data:
            if not isinstance(entry, dict) or "date" not in entry or "kWh" not in entry:
                continue
                
            try:
                # Parse timestamp
                date_str = entry["date"]
                if date_str.endswith('Z'):
                    date_str = date_str[:-1] + '+00:00'
                timestamp = datetime.fromisoformat(date_str)
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=ZoneInfo("UTC"))
                
                # Convert to local time for statistics
                local_time = timestamp.astimezone(self.local_tz)
                
                # Add to running total
                kwh = entry["kWh"]
                running_total += kwh
                
                # Create statistic data point
                statistics.append(StatisticData(
                    start=local_time,
                    state=running_total,
                    sum=running_total
                ))
                
            except Exception as e:
                _LOGGER.warning("Error processing consumption entry: %s", e)
                
        if statistics:
            # Add statistics to recorder
            metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name="Ostrom Energy Consumption",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            
            try:
                async_add_external_statistics(self.hass, metadata, statistics)
                _LOGGER.debug("Added %d historical consumption statistics", len(statistics))
            except Exception as e:
                _LOGGER.error("Failed to add historical statistics: %s", e)

class OstromForecastSensor(CoordinatorEntity, SensorEntity):
    """Sensor for price forecasting."""
    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_translation_key = "ostrom_integration_spot_price"
        self._attr_unique_id = f"ostrom_spot_price_{entry.data['zip_code']}"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "€/kWh"
        self._attr_should_poll = False
        self._attr_suggested_display_precision = 2
        self._attr_device_info = coordinator.device_info

    @property
    def force_update(self) -> bool:
        """Force update for every update interval."""
        return True

    @property
    def state_attributes(self):
        """Return the state attributes."""
        attrs = super().state_attributes or {}
        attrs["last_reset"] = None
        return attrs

    @property
    def native_value(self) -> Optional[float]:
        """Return the current price."""
        if not self.coordinator.data:
            return None
        current_price = self.coordinator.data.get_current_price()
        return round(current_price, 4) if current_price is not None else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        if not self.coordinator.data:
            return {}

        data = self.coordinator.data
        
        # Add all prices to attributes for easy querying
        prices = {}
        for timestamp, price in data.prices.items():
            # Convert UTC to local time for easier use in automations
            local_time = timestamp.astimezone(self.coordinator.local_tz)
            prices[local_time.isoformat()] = round(price, 4)

        attributes = {
            "average_price": round(data._avg_price, 4),
            "min_price": round(data._min_price, 4),
            "max_price": round(data._max_price, 4),
            "next_price": round(data.next_price, 4) if data.next_price else None,
            "lowest_price_time": data.lowest_price_time.astimezone(self.coordinator.local_tz).isoformat() if data.lowest_price_time else None,
            "highest_price_time": data.highest_price_time.astimezone(self.coordinator.local_tz).isoformat() if data.highest_price_time else None,
            "prices": prices,  # Add all prices
            "current_hour": datetime.now(self.coordinator.local_tz).strftime("%H:00"),
        }

        return attributes

    def get_price_at_time(self, time_str: str) -> Optional[float]:
        """Get price for a specific time (format: HH:MM)."""
        if not self.coordinator.data:
            return None

        try:
            # Parse the time string
            now = datetime.now(self.coordinator.local_tz)
            hour, minute = map(int, time_str.split(':'))
            target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            
            # If the time is in the past for today, assume tomorrow
            if target_time < now:
                target_time += timedelta(days=1)

            # Convert to UTC for lookup
            utc_time = target_time.astimezone(ZoneInfo("UTC"))
            utc_time = utc_time.replace(minute=0)  # Round to hour

            return self.coordinator.data.prices.get(utc_time)
        except (ValueError, TypeError):
            return None

    def is_price_below(self, price_threshold: float, time_str: Optional[str] = None) -> bool:
        """Check if price is below threshold at given time or current time."""
        if time_str:
            price = self.get_price_at_time(time_str)
        else:
            price = self.native_value
            
        if price is None:
            return False
            
        return price <= price_threshold

class OstromAveragePriceSensor(CoordinatorEntity, SensorEntity):
    """Sensor for average price."""
    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_translation_key = "ostrom_integration_average_price"
        self._attr_unique_id = f"ostrom_average_price_{entry.data['zip_code']}"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "€/kWh"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = coordinator.device_info

    @property
    def native_value(self) -> Optional[float]:
        if not self.coordinator.data:
            return None
        return round(self.coordinator.data._avg_price, 4) if self.coordinator.data._avg_price else None

class OstromMinPriceSensor(CoordinatorEntity, SensorEntity):
    """Sensor for minimum price."""
    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_translation_key = "ostrom_integration_lowest_price"
        self._attr_unique_id = f"ostrom_min_price_{entry.data['zip_code']}"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "€/kWh"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = coordinator.device_info

    @property
    def native_value(self) -> Optional[float]:
        if not self.coordinator.data:
            return None
        return round(self.coordinator.data._min_price, 4) if self.coordinator.data._min_price else None

class OstromMaxPriceSensor(CoordinatorEntity, SensorEntity):
    """Sensor for maximum price."""
    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_translation_key = "ostrom_integration_highest_price"
        self._attr_unique_id = f"ostrom_max_price_{entry.data['zip_code']}"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "€/kWh"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = coordinator.device_info

    @property
    def native_value(self) -> Optional[float]:
        if not self.coordinator.data:
            return None
        return round(self.coordinator.data._max_price, 4) if self.coordinator.data._max_price else None

class OstromNextPriceSensor(CoordinatorEntity, SensorEntity):
    """Sensor for next hour's price."""
    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_translation_key = "ostrom_integration_next_hour_price"
        self._attr_unique_id = f"ostrom_next_price_{entry.data['zip_code']}"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "€/kWh"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = coordinator.device_info
        self.entity_id = f"sensor.{DOMAIN}_{self._attr_translation_key}"

    @property
    def native_value(self) -> Optional[float]:
        if not self.coordinator.data:
            return None
        return round(self.coordinator.data.next_price, 4) if self.coordinator.data.next_price else None

class OstromLowestPriceTimeSensor(CoordinatorEntity, SensorEntity):
    """Sensor for lowest price time."""
    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = f"ostrom_lowest_price_time_{entry.data['zip_code']}"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_device_info = coordinator.device_info
        self._attr_entity_registry_enabled_default = True
        self._attr_has_entity_name = True
        self._attr_translation_key = "ostrom_integration_lowest_price_time"

    @property
    def native_value(self) -> Optional[datetime]:
        """Return the lowest price time in the device's local timezone."""
        if not self.coordinator.data or not self.coordinator.data.lowest_price_time:
            return None
        
        # Ensure UTC timezone if not set
        time = self.coordinator.data.lowest_price_time
        if time.tzinfo is None:
            time = time.replace(tzinfo=ZoneInfo("UTC"))
            
        # Convert to local timezone
        return time.astimezone(self.coordinator.local_tz)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return formatted time as an attribute."""
        if not self.native_value:
            return {}
            
        local_time = self.native_value
        return {
            "formatted_time": local_time.strftime("%I:%M %p"),
            "time_date": local_time.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "is_today": local_time.date() == datetime.now(self.coordinator.local_tz).date(),
            "is_tomorrow": local_time.date() == (datetime.now(self.coordinator.local_tz) + timedelta(days=1)).date()
        }

class OstromHighestPriceTimeSensor(CoordinatorEntity, SensorEntity):
    """Sensor for highest price time."""
    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = f"ostrom_highest_price_time_{entry.data['zip_code']}"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_device_info = coordinator.device_info
        self._attr_entity_registry_enabled_default = True
        self._attr_has_entity_name = True
        self._attr_translation_key = "ostrom_integration_highest_price_time"

    @property
    def native_value(self) -> Optional[datetime]:
        """Return the highest price time in the device's local timezone."""
        if not self.coordinator.data or not self.coordinator.data.highest_price_time:
            return None
        
        # Ensure UTC timezone if not set
        time = self.coordinator.data.highest_price_time
        if time.tzinfo is None:
            time = time.replace(tzinfo=ZoneInfo("UTC"))
            
        # Convert to local timezone
        return time.astimezone(self.coordinator.local_tz)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return formatted time as an attribute."""
        if not self.native_value:
            return {}
            
        local_time = self.native_value
        return {
            "formatted_time": local_time.strftime("%I:%M %p"),
            "time_date": local_time.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "is_today": local_time.date() == datetime.now(self.coordinator.local_tz).date(),
            "is_tomorrow": local_time.date() == (datetime.now(self.coordinator.local_tz) + timedelta(days=1)).date()
        }

class OstromConsumptionSensor(CoordinatorEntity, SensorEntity):
    """Sensor for current energy consumption."""
    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_translation_key = "ostrom_integration_energy_consumption"
        self._attr_unique_id = f"ostrom_consumption_{entry.data['zip_code']}"
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_suggested_display_precision = 2
        self._attr_device_info = coordinator.device_info
        self._attr_last_reset = None  # Will be set when data is available

    @property
    def native_value(self) -> Optional[float]:
        """Return the running total consumption value."""
        if not self.coordinator.data or not self.coordinator.data.consumption_data:
            return None
        
        consumption_data = self.coordinator.data.consumption_data
        if isinstance(consumption_data, dict) and "data" in consumption_data:
            # Calculate running total of consumption from hourly data
            hourly_data = consumption_data["data"]
            if isinstance(hourly_data, list) and len(hourly_data) > 0:
                running_total = sum(entry.get("kWh", 0) for entry in hourly_data if isinstance(entry, dict))
                return round(running_total, 3)
        
        return None

    @property 
    def last_reset(self) -> Optional[datetime]:
        """Return the last reset time for this sensor."""
        if not self.coordinator.data or not self.coordinator.data.consumption_data:
            return None
            
        consumption_data = self.coordinator.data.consumption_data
        if isinstance(consumption_data, dict) and "data" in consumption_data:
            hourly_data = consumption_data["data"]
            if isinstance(hourly_data, list) and len(hourly_data) > 0:
                # Use the first hour as the reset time
                first_entry = hourly_data[0]
                if isinstance(first_entry, dict) and "date" in first_entry:
                    try:
                        date_str = first_entry["date"]
                        if date_str.endswith('Z'):
                            date_str = date_str[:-1] + '+00:00'
                        timestamp = datetime.fromisoformat(date_str)
                        if timestamp.tzinfo is None:
                            timestamp = timestamp.replace(tzinfo=ZoneInfo("UTC"))
                        return timestamp.astimezone(self.coordinator.local_tz)
                    except Exception:
                        pass
        return None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        if not self.coordinator.data or not self.coordinator.data.consumption_data:
            return {}

        consumption_data = self.coordinator.data.consumption_data
        if isinstance(consumption_data, dict) and "data" in consumption_data:
            hourly_data = consumption_data["data"]
            if isinstance(hourly_data, list) and len(hourly_data) > 0:
                # Create hourly consumption breakdown
                hourly_consumption = {}
                total_hours = len(hourly_data)
                min_consumption = float('inf')
                max_consumption = 0
                
                for entry in hourly_data:
                    if isinstance(entry, dict) and "date" in entry and "kWh" in entry:
                        # Parse date and convert to local time
                        try:
                            date_str = entry["date"]
                            if date_str.endswith('Z'):
                                date_str = date_str[:-1] + '+00:00'
                            timestamp = datetime.fromisoformat(date_str)
                            if timestamp.tzinfo is None:
                                timestamp = timestamp.replace(tzinfo=ZoneInfo("UTC"))
                            local_time = timestamp.astimezone(self.coordinator.local_tz)
                            
                            kwh = entry["kWh"]
                            hourly_consumption[local_time.strftime("%H:%M")] = kwh
                            
                            if kwh < min_consumption:
                                min_consumption = kwh
                            if kwh > max_consumption:
                                max_consumption = kwh
                        except Exception as e:
                            _LOGGER.warning("Error parsing consumption entry: %s", e)
                
                attributes = {
                    "hourly_consumption": hourly_consumption,
                    "total_hours": total_hours,
                    "min_hourly_consumption": min_consumption if min_consumption != float('inf') else None,
                    "max_hourly_consumption": max_consumption if max_consumption > 0 else None,
                    "average_hourly_consumption": round(self.native_value / total_hours, 3) if self.native_value and total_hours > 0 else None,
                    "data_date": hourly_data[0].get("date", "").split("T")[0] if hourly_data else None
                }
                
                return attributes
        
        return {}

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return (
            self.coordinator.last_update_success 
            and self.coordinator.data is not None
            and hasattr(self.coordinator, 'contract_id')
            and self.coordinator.contract_id is not None
        )
"""Test BLE advertisement data parsing."""

import pytest

from opendisplay.models.advertisement import (
    AdvertisementData,
    AdvertisementTracker,
    TouchTracker,
    decode_button_event,
    decode_sht40,
    parse_advertisement,
)


def _v1_payload(
    dynamic_data: bytes,
    *,
    temperature_c: float = 22.0,
    battery_mv: int = 3950,
    reboot_flag: bool = False,
    connection_requested: bool = False,
    loop_counter: int = 0,
) -> bytes:
    """Build v1-format advertisement payload (without manufacturer ID)."""
    if len(dynamic_data) != 11:
        raise ValueError("dynamic_data must be exactly 11 bytes")

    temp_encoded = int(round((temperature_c + 40.0) * 2.0))
    temp_encoded = max(0, min(255, temp_encoded))

    battery_10mv = max(0, min(511, battery_mv // 10))
    battery_low = battery_10mv & 0xFF
    battery_high = (battery_10mv >> 8) & 0x01

    status = battery_high
    if reboot_flag:
        status |= 0x02
    if connection_requested:
        status |= 0x04
    status |= (loop_counter & 0x0F) << 4

    return dynamic_data + bytes([temp_encoded, battery_low, status])


class TestParseAdvertisement:
    """Test BLE advertisement data parsing."""

    def test_parse_advertisement_valid(self):
        """Test parsing valid 11-byte advertisement data."""
        # Real format (manufacturer ID stripped by Bleak):
        # [protocol:7][battery:2 LE][temp:1 signed][loop:1]
        # Battery: 3925mV (0x0f55), Temp: 22°C, Loop: 77
        data = bytes([0x02, 0x36, 0x00, 0x6C, 0x00, 0xC3, 0x01, 0x55, 0x0F, 0x16, 0x4D])

        result = parse_advertisement(data)

        assert isinstance(result, AdvertisementData)
        assert result.battery_mv == 3925
        assert result.temperature_c == 22
        assert result.loop_counter == 77
        assert result.format_version == "legacy"
        assert result.reboot_flag is None
        assert result.connection_requested is None
        assert result.dynamic_data == b""

    def test_parse_advertisement_different_values(self):
        """Test parsing with different sensor values."""
        # Battery: 4200mV (0x1068), Temp: 25°C (0x19), Loop: 100 (0x64)
        data = bytes([0x02, 0x36, 0x00, 0x6C, 0x00, 0xC3, 0x01, 0x68, 0x10, 0x19, 0x64])

        result = parse_advertisement(data)

        assert result.battery_mv == 4200
        assert result.temperature_c == 25
        assert result.loop_counter == 100

    def test_parse_advertisement_low_battery(self):
        """Test parsing with low battery voltage."""
        # Battery: 2800mV (0x0af0), Temp: 20°C, Loop: 50
        data = bytes([0x02, 0x36, 0x00, 0x6C, 0x00, 0xC3, 0x01, 0xF0, 0x0A, 0x14, 0x32])

        result = parse_advertisement(data)

        assert result.battery_mv == 2800
        assert result.temperature_c == 20
        assert result.loop_counter == 50

    def test_parse_advertisement_negative_temperature(self):
        """Test parsing with negative temperature."""
        # Battery: 3000mV, Temp: -5°C (0xfb = -5 in signed int8), Loop: 10
        data = bytes([0x02, 0x36, 0x00, 0x6C, 0x00, 0xC3, 0x01, 0xB8, 0x0B, 0xFB, 0x0A])

        result = parse_advertisement(data)

        assert result.battery_mv == 3000
        assert result.temperature_c == -5
        assert result.loop_counter == 10

    def test_parse_advertisement_too_short(self):
        """Test that too-short data raises ValueError."""
        data = bytes([0x02, 0x36, 0x00, 0x6C, 0x00])  # Only 5 bytes

        with pytest.raises(ValueError, match="too short.*11"):
            parse_advertisement(data)

    def test_parse_advertisement_empty(self):
        """Test that empty data raises ValueError."""
        with pytest.raises(ValueError, match="too short"):
            parse_advertisement(bytes())

    def test_parse_advertisement_loop_counter_overflow(self):
        """Test loop counter wrapping at 255."""
        # Loop counter at max value (255 = 0xff)
        data = bytes([0x02, 0x36, 0x00, 0x6C, 0x00, 0xC3, 0x01, 0x55, 0x0F, 0x16, 0xFF])

        result = parse_advertisement(data)

        assert result.loop_counter == 255

    def test_parse_advertisement_v1_format(self):
        """Test parsing v1 (firmware 1.0+) 14-byte advertisement data."""
        # dynamic_data[0:11]
        # temperature: 22.0C -> (22 + 40) * 2 = 124 (0x7c)
        # battery: 3.95V -> 3950mV -> 395 x 10mV units -> low=0x8b, high bit=1
        # status: bit0=batt_msb(1), bit1=reboot(1), bit2=conn_req(0), bits4-7=loop(5)
        data = bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 0x7C, 0x8B, 0x53])

        result = parse_advertisement(data)

        assert result.format_version == "v1"
        assert result.temperature_c == 22.0
        assert result.battery_mv == 3950
        assert result.loop_counter == 5
        assert result.reboot_flag is True
        assert result.connection_requested is False
        assert result.dynamic_data == bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11])

    def test_parse_advertisement_strips_manufacturer_id_for_legacy(self):
        """Parser should accept payloads with manufacturer ID included."""
        # Manufacturer ID 0x2446 (little-endian), followed by legacy 11-byte payload
        payload = bytes([0x46, 0x24, 0x02, 0x36, 0x00, 0x6C, 0x00, 0xC3, 0x01, 0x55, 0x0F, 0x16, 0x4D])

        result = parse_advertisement(payload)

        assert result.format_version == "legacy"
        assert result.battery_mv == 3925
        assert result.temperature_c == 22
        assert result.loop_counter == 77

    def test_parse_advertisement_strips_manufacturer_id_for_v1(self):
        """Parser should accept v1 payloads with manufacturer ID included."""
        payload = bytes([0x46, 0x24, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 0x7C, 0x8B, 0x53])

        result = parse_advertisement(payload)

        assert result.format_version == "v1"
        assert result.battery_mv == 3950
        assert result.temperature_c == 22.0
        assert result.loop_counter == 5

    def test_parse_advertisement_rejects_unknown_legacy_signature(self):
        """11-byte payload with unknown signature should be rejected."""
        data = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF, 0x00, 0x55, 0x0F, 0x16, 0x4D])

        with pytest.raises(ValueError, match="Unsupported legacy advertisement signature"):
            parse_advertisement(data)

    def test_decode_button_event_helper(self):
        """Decode packed button fields from one v1 dynamic byte."""
        # button_id=3, press_count=5, pressed=1
        decoded = decode_button_event(0xAB, byte_index=2)

        assert decoded.byte_index == 2
        assert decoded.raw == 0xAB
        assert decoded.button_id == 3
        assert decoded.press_count == 5
        assert decoded.pressed is True

    def test_advertisement_button_helpers_v1(self):
        """v1 advertisements expose convenience helpers."""
        payload = _v1_payload(
            bytes([0xAB]) + bytes(10),
            temperature_c=21.5,
            battery_mv=3890,
            loop_counter=7,
        )
        adv = parse_advertisement(payload)

        event = adv.button_event(0)
        assert event is not None
        assert event.button_id == 3
        assert event.press_count == 5
        assert event.pressed is True
        assert adv.is_pressed(0) is True
        assert len(adv.button_events) == 11

    def test_advertisement_button_helpers_legacy(self):
        """Legacy advertisements return no button convenience data."""
        data = bytes([0x02, 0x36, 0x00, 0x6C, 0x00, 0xC3, 0x01, 0x55, 0x0F, 0x16, 0x4D])
        adv = parse_advertisement(data)

        assert adv.button_event(0) is None
        assert adv.is_pressed(0) is None
        assert adv.button_events == []

    def test_advertisement_tracker_emits_button_events(self):
        """Tracker emits edge/count events on v1 payload transitions."""
        tracker = AdvertisementTracker()
        address = "AA:BB:CC:DD:EE:FF"

        first = parse_advertisement(_v1_payload(bytes([0x0A]) + bytes(10), loop_counter=1))  # id=2, count=1, up
        second = parse_advertisement(_v1_payload(bytes([0x92]) + bytes(10), loop_counter=2))  # id=2, count=2, down
        third = parse_advertisement(_v1_payload(bytes([0x12]) + bytes(10), loop_counter=3))  # id=2, count=2, up

        assert tracker.update(address, first, timestamp=1.0) == []

        second_events = tracker.update(address, second, timestamp=2.0)
        assert [e.event_type for e in second_events] == ["button_down", "press_count_changed"]
        assert second_events[0].button_id == 2
        assert second_events[0].pressed is True
        assert second_events[1].previous_press_count == 1
        assert second_events[1].press_count == 2

        third_events = tracker.update(address, third, timestamp=3.0)
        assert [e.event_type for e in third_events] == ["button_up"]
        assert third_events[0].button_id == 2
        assert third_events[0].pressed is False

    def test_advertisement_tracker_emits_synthetic_press_on_missed_short_press(self):
        """Count can increase while released if press+release fell between adverts."""
        tracker = AdvertisementTracker()
        address = "AA:BB:CC:DD:EE:FF"

        first = parse_advertisement(_v1_payload(bytes([0x0A]) + bytes(10), loop_counter=1))  # id=2, count=1, up
        second = parse_advertisement(_v1_payload(bytes([0x12]) + bytes(10), loop_counter=2))  # id=2, count=2, up

        assert tracker.update(address, first, timestamp=1.0) == []

        events = tracker.update(address, second, timestamp=2.0)
        assert [e.event_type for e in events] == ["button_down", "button_up", "press_count_changed"]
        assert events[0].button_id == 2
        assert events[0].pressed is True
        assert events[1].pressed is False
        assert events[2].press_count == 2
        assert events[2].previous_press_count == 1

    def test_advertisement_tracker_emits_synthetic_press_on_missed_slot_change(self):
        """Another button in the group may tap between adverts without a held state."""
        tracker = AdvertisementTracker()
        address = "AA:BB:CC:DD:EE:FF"

        first = parse_advertisement(_v1_payload(bytes([0x0A]) + bytes(10), loop_counter=1))  # id=2, count=1, up
        second = parse_advertisement(_v1_payload(bytes([0x09]) + bytes(10), loop_counter=2))  # id=1, count=1, up

        assert tracker.update(address, first, timestamp=1.0) == []

        events = tracker.update(address, second, timestamp=2.0)
        assert [e.event_type for e in events] == [
            "button_slot_changed",
            "button_down",
            "button_up",
        ]
        assert events[0].button_id == 1
        assert events[1].pressed is True
        assert events[2].pressed is False


class TestTouchEventData:
    """Test TouchEventData parsing from dynamic data bytes."""

    def _adv_with_touch(
        self, contact_count: int, track_id: int, x: int, y: int, start_byte: int = 0
    ) -> AdvertisementData:
        """Build a v1 advertisement with touch data at the given offset."""
        dynamic = bytearray(11)
        dynamic[start_byte] = (contact_count & 0x0F) | ((track_id & 0x0F) << 4)
        dynamic[start_byte + 1] = x & 0xFF
        dynamic[start_byte + 2] = (x >> 8) & 0xFF
        dynamic[start_byte + 3] = y & 0xFF
        dynamic[start_byte + 4] = (y >> 8) & 0xFF
        payload = _v1_payload(bytes(dynamic))
        return parse_advertisement(payload)

    def test_touch_event_idle(self) -> None:
        """contact_count=0 → touch_idle, coordinates accessible."""
        adv = self._adv_with_touch(contact_count=0, track_id=0, x=100, y=200)
        event = adv.touch_event(0)
        assert event is not None
        assert event.contact_count == 0
        assert event.event_type == "touch_idle"
        assert not event.is_touching

    def test_touch_event_active(self) -> None:
        """contact_count=1 → touch_down with correct x/y/track_id."""
        adv = self._adv_with_touch(contact_count=1, track_id=3, x=320, y=240)
        event = adv.touch_event(0)
        assert event is not None
        assert event.contact_count == 1
        assert event.track_id == 3
        assert event.x == 320
        assert event.y == 240
        assert event.event_type == "touch_down"
        assert event.is_touching

    def test_touch_event_released(self) -> None:
        """contact_count=6 → touch_up (released), coordinates latched."""
        adv = self._adv_with_touch(contact_count=6, track_id=1, x=50, y=75)
        event = adv.touch_event(0)
        assert event is not None
        assert event.contact_count == 6
        assert event.event_type == "touch_up"
        assert not event.is_touching
        assert event.x == 50
        assert event.y == 75

    def test_touch_event_non_zero_start_byte(self) -> None:
        """Touch data at start_byte=3 is parsed from the correct offset."""
        adv = self._adv_with_touch(contact_count=2, track_id=0, x=128, y=64, start_byte=3)
        event = adv.touch_event(3)
        assert event is not None
        assert event.contact_count == 2
        assert event.x == 128
        assert event.y == 64

    def test_touch_event_returns_none_for_legacy(self) -> None:
        """Legacy advertisements return None for touch_event()."""
        data = bytes([0x02, 0x36, 0x00, 0x6C, 0x00, 0xC3, 0x01, 0x55, 0x0F, 0x16, 0x4D])
        adv = parse_advertisement(data)
        assert adv.touch_event(0) is None

    def test_touch_event_out_of_range_returns_none(self) -> None:
        """start_byte too large to fit 5-byte block returns None."""
        payload = _v1_payload(bytes(11))
        adv = parse_advertisement(payload)
        assert adv.touch_event(7) is None  # 7+5=12 > 11

    def test_touch_event_max_contacts(self) -> None:
        """contact_count=5 (max) is still touch_down."""
        adv = self._adv_with_touch(contact_count=5, track_id=0, x=10, y=10)
        event = adv.touch_event(0)
        assert event is not None
        assert event.event_type == "touch_down"
        assert event.is_touching


class TestTouchTracker:
    """Test TouchTracker state machine transitions."""

    ADDRESS = "AA:BB:CC:DD:EE:FF"

    def _adv(self, contact_count: int, x: int = 0, y: int = 0) -> AdvertisementData:
        """Build v1 advertisement with touch data at start_byte=0."""
        dynamic = bytearray(11)
        dynamic[0] = contact_count & 0x0F
        dynamic[1] = x & 0xFF
        dynamic[2] = (x >> 8) & 0xFF
        dynamic[3] = y & 0xFF
        dynamic[4] = (y >> 8) & 0xFF
        return parse_advertisement(_v1_payload(bytes(dynamic)))

    def test_first_advertisement_emits_no_events(self) -> None:
        """First update seeds state; no events emitted."""
        tracker = TouchTracker(instance=0, start_byte=0)
        adv = self._adv(contact_count=0)
        assert tracker.update(self.ADDRESS, adv, timestamp=1.0) == []

    def test_touch_down_on_idle_to_active(self) -> None:
        """Transition from idle (0) to active (1) emits touch_down."""
        tracker = TouchTracker(instance=0, start_byte=0)
        tracker.update(self.ADDRESS, self._adv(0), timestamp=1.0)

        events = tracker.update(self.ADDRESS, self._adv(1, x=100, y=200), timestamp=2.0)
        assert len(events) == 1
        assert events[0].event_type == "touch_down"
        assert events[0].x == 100
        assert events[0].y == 200
        assert events[0].instance == 0
        assert events[0].address == self.ADDRESS
        assert events[0].timestamp == 2.0

    def test_touch_up_on_active_to_released(self) -> None:
        """Transition from active to released (6) emits touch_up."""
        tracker = TouchTracker(instance=0, start_byte=0)
        tracker.update(self.ADDRESS, self._adv(0), timestamp=1.0)
        tracker.update(self.ADDRESS, self._adv(1, x=50, y=50), timestamp=2.0)

        events = tracker.update(self.ADDRESS, self._adv(6, x=50, y=50), timestamp=3.0)
        assert len(events) == 1
        assert events[0].event_type == "touch_up"

    def test_touch_up_on_active_to_idle(self) -> None:
        """Transition directly from active to idle also emits touch_up."""
        tracker = TouchTracker(instance=0, start_byte=0)
        tracker.update(self.ADDRESS, self._adv(0), timestamp=1.0)
        tracker.update(self.ADDRESS, self._adv(1, x=10, y=20), timestamp=2.0)

        events = tracker.update(self.ADDRESS, self._adv(0), timestamp=3.0)
        assert len(events) == 1
        assert events[0].event_type == "touch_up"

    def test_touch_move_on_position_change(self) -> None:
        """Active→active with different coordinates emits touch_move."""
        tracker = TouchTracker(instance=0, start_byte=0)
        tracker.update(self.ADDRESS, self._adv(0), timestamp=1.0)
        tracker.update(self.ADDRESS, self._adv(1, x=100, y=100), timestamp=2.0)

        events = tracker.update(self.ADDRESS, self._adv(1, x=150, y=120), timestamp=3.0)
        assert len(events) == 1
        assert events[0].event_type == "touch_move"
        assert events[0].x == 150
        assert events[0].y == 120

    def test_no_event_on_same_position(self) -> None:
        """Active→active with same coordinates emits no event."""
        tracker = TouchTracker(instance=0, start_byte=0)
        tracker.update(self.ADDRESS, self._adv(0), timestamp=1.0)
        tracker.update(self.ADDRESS, self._adv(1, x=100, y=100), timestamp=2.0)

        events = tracker.update(self.ADDRESS, self._adv(1, x=100, y=100), timestamp=3.0)
        assert events == []

    def test_no_event_on_idle_to_idle(self) -> None:
        """Idle→idle emits no event."""
        tracker = TouchTracker(instance=0, start_byte=0)
        tracker.update(self.ADDRESS, self._adv(0), timestamp=1.0)
        events = tracker.update(self.ADDRESS, self._adv(0), timestamp=2.0)
        assert events == []

    def test_instance_number_in_event(self) -> None:
        """TouchChangeEvent carries the correct instance number."""
        tracker = TouchTracker(instance=2, start_byte=3)
        dynamic = bytearray(11)
        dynamic[3] = 1  # contact_count=1 at start_byte=3
        adv_idle = parse_advertisement(_v1_payload(bytes(11)))
        adv_touch = parse_advertisement(_v1_payload(bytes(dynamic)))

        tracker.update(self.ADDRESS, adv_idle, timestamp=1.0)
        events = tracker.update(self.ADDRESS, adv_touch, timestamp=2.0)
        assert len(events) == 1
        assert events[0].instance == 2

    def test_reset_clears_state(self) -> None:
        """After reset, first update for that address seeds state again."""
        tracker = TouchTracker(instance=0, start_byte=0)
        tracker.update(self.ADDRESS, self._adv(0), timestamp=1.0)
        tracker.reset(self.ADDRESS)

        # After reset, this should be treated as the first advertisement → no event
        events = tracker.update(self.ADDRESS, self._adv(1, x=10, y=10), timestamp=2.0)
        assert events == []

    def test_reset_all_clears_every_address(self) -> None:
        """reset() with no argument clears state for all tracked addresses."""
        other = "11:22:33:44:55:66"
        tracker = TouchTracker(instance=0, start_byte=0)
        tracker.update(self.ADDRESS, self._adv(0), timestamp=1.0)
        tracker.update(other, self._adv(0), timestamp=1.0)
        tracker.reset()

        # Both addresses should be treated as first advertisement → no events
        assert tracker.update(self.ADDRESS, self._adv(1, x=5, y=5), timestamp=2.0) == []
        assert tracker.update(other, self._adv(1, x=5, y=5), timestamp=2.0) == []

    def test_legacy_advertisement_returns_no_events(self) -> None:
        """Legacy advertisements (no dynamic_data) produce no touch events."""
        tracker = TouchTracker(instance=0, start_byte=0)
        legacy = bytes([0x02, 0x36, 0x00, 0x6C, 0x00, 0xC3, 0x01, 0x55, 0x0F, 0x16, 0x4D])
        adv = parse_advertisement(legacy)
        assert tracker.update(self.ADDRESS, adv, timestamp=1.0) == []


# ── tracker byte filtering ────────────────────────────────────────────────────


def _dynamic(**by_index: int) -> bytes:
    """An 11-byte dynamic block with the given bytes set."""
    block = bytearray(11)
    for index, value in by_index.items():
        block[int(index.removeprefix("b"))] = value
    return bytes(block)


def test_tracker_ignores_bytes_no_button_reports_into() -> None:
    """A sensor or touch byte changing must not look like a button press.

    Byte 1 here stands in for a slot owned by something else (an SHT40 block
    or a touch coordinate). Only byte 0 is a configured button.
    """
    tracker = AdvertisementTracker([0])
    tracker.update("AA", parse_advertisement(_v1_payload(_dynamic(b0=0x28, b1=0x54))))

    events = tracker.update("AA", parse_advertisement(_v1_payload(_dynamic(b0=0x28, b1=0x4C))))

    assert events == []


def test_tracker_without_indices_still_watches_every_byte() -> None:
    """Historical behaviour is preserved when no indices are given."""
    tracker = AdvertisementTracker()
    tracker.update("AA", parse_advertisement(_v1_payload(_dynamic(b1=0x54))))

    events = tracker.update("AA", parse_advertisement(_v1_payload(_dynamic(b1=0x4C))))

    assert [e.byte_index for e in events] == [1]


def test_tracker_still_reports_configured_button() -> None:
    """Filtering must not suppress the bytes that are real buttons."""
    tracker = AdvertisementTracker([0])
    tracker.update("AA", parse_advertisement(_v1_payload(_dynamic(b0=0x00))))

    events = tracker.update("AA", parse_advertisement(_v1_payload(_dynamic(b0=0x88))))

    assert [(e.event_type, e.byte_index) for e in events] == [
        ("button_down", 0),
        ("press_count_changed", 0),
    ]


def test_tracker_watches_several_button_bytes() -> None:
    tracker = AdvertisementTracker([0, 5])
    tracker.update("AA", parse_advertisement(_v1_payload(_dynamic(b0=0x00, b3=0x11, b5=0x00))))

    events = tracker.update("AA", parse_advertisement(_v1_payload(_dynamic(b0=0x88, b3=0x99, b5=0x88))))

    assert sorted(e.byte_index for e in events) == [0, 0, 5, 5]


def test_tracker_accepts_any_iterable_of_indices() -> None:
    tracker = AdvertisementTracker(i for i in (0,))
    tracker.update("AA", parse_advertisement(_v1_payload(_dynamic(b0=0x00))))

    events = tracker.update("AA", parse_advertisement(_v1_payload(_dynamic(b0=0x88))))

    assert [e.byte_index for e in events] == [0, 0]


def test_tracker_with_no_button_bytes_emits_nothing() -> None:
    """A device whose inputs all publish nothing yields no events at all."""
    tracker = AdvertisementTracker([])
    tracker.update("AA", parse_advertisement(_v1_payload(_dynamic(b0=0x28))))

    assert tracker.update("AA", parse_advertisement(_v1_payload(_dynamic(b0=0x4C)))) == []


# ── SHT40 sensor readings ─────────────────────────────────────────────────────


def _encode_sht40(temp_deci: int, rh_centi: int) -> bytes:
    """Encode a reading exactly as the firmware does (sensor_sht40.cpp)."""
    temp_units = temp_deci + 400
    rh_deci = (rh_centi + 5) // 10
    packed = (rh_deci & 0x3FF) | (temp_units << 10)
    return bytes([packed & 0xFF, (packed >> 8) & 0xFF, (packed >> 16) & 0xFF])


def _dynamic_with_sht40(block: bytes, start_byte: int = 7) -> bytes:
    """An 11-byte dynamic block carrying a 3-byte SHT40 reading."""
    dynamic = bytearray(11)
    dynamic[start_byte : start_byte + 3] = block
    return bytes(dynamic)


def test_decode_sht40_known_vector() -> None:
    """Decodes the firmware's own encoding of 22.4 C / 47.1 %RH."""
    reading = decode_sht40(bytes.fromhex("d7c109"))

    assert reading is not None
    assert reading.temperature_c == pytest.approx(22.4)
    assert reading.humidity_percent == pytest.approx(47.1)
    assert reading.start_byte == 7
    assert reading.raw == bytes.fromhex("d7c109")


@pytest.mark.parametrize(
    ("temp_deci", "rh_centi"),
    [
        (-400, 100),  # temperature floor
        (1250, 10000),  # both range ceilings
        (0, 0),  # humidity floor
        (0, 5000),
        (224, 4712),
        (-123, 8888),
    ],
)
def test_decode_sht40_round_trips_firmware_encoding(temp_deci: int, rh_centi: int) -> None:
    """Every value the firmware can encode decodes back to itself."""
    reading = decode_sht40(_encode_sht40(temp_deci, rh_centi))

    assert reading is not None
    assert reading.temperature_c == pytest.approx(temp_deci / 10.0)
    assert reading.humidity_percent == pytest.approx(round(rh_centi / 10) / 10.0)


def test_decode_sht40_read_failure_sentinel() -> None:
    """FF FF FF is the firmware's explicit read-failure marker, not a reading."""
    assert decode_sht40(b"\xff\xff\xff") is None


def test_decode_sht40_unwritten_slot() -> None:
    """00 00 00 decodes to a plausible -40 C / 0 %RH but means 'never written'.

    This is the one lossy case: a real -40.0 C at exactly 0 %RH encodes to the
    same three zero bytes as a slot the firmware never touched. Reporting it is
    the worse failure -- it would write a hard -40 C into consumers' long-term
    statistics -- so the ambiguous pattern is treated as "no reading".
    """
    assert decode_sht40(b"\x00\x00\x00") is None
    assert decode_sht40(_encode_sht40(-400, 0)) is None


def test_decode_sht40_rejects_wrong_length() -> None:
    with pytest.raises(ValueError, match="must be 3 bytes"):
        decode_sht40(b"\x01\x02")


def test_sht40_reading_from_advertisement() -> None:
    """A v1 advertisement decodes the block at the configured offset."""
    payload = _v1_payload(_dynamic_with_sht40(bytes.fromhex("d7c109"), start_byte=7))

    reading = parse_advertisement(payload).sht40_reading(7)

    assert reading is not None
    assert reading.temperature_c == pytest.approx(22.4)
    assert reading.humidity_percent == pytest.approx(47.1)


def test_sht40_reading_defaults_to_firmware_slot() -> None:
    """The default start byte matches the firmware's own default of 7."""
    payload = _v1_payload(_dynamic_with_sht40(bytes.fromhex("d7c109"), start_byte=7))

    assert parse_advertisement(payload).sht40_reading() is not None


def test_sht40_reading_at_relocated_offset() -> None:
    """Config can move the block anywhere that fits in the dynamic area."""
    payload = _v1_payload(_dynamic_with_sht40(bytes.fromhex("d7c109"), start_byte=0))
    adv = parse_advertisement(payload)

    assert adv.sht40_reading(0) is not None
    assert adv.sht40_reading(7) is None  # zeroed elsewhere → unwritten


@pytest.mark.parametrize("start_byte", [-1, 9, 11, 99])
def test_sht40_reading_rejects_out_of_range_offset(start_byte: int) -> None:
    """A 3-byte block must fit the 11-byte dynamic area (firmware caps start at 8)."""
    payload = _v1_payload(_dynamic_with_sht40(bytes.fromhex("d7c109")))

    assert parse_advertisement(payload).sht40_reading(start_byte) is None


def test_sht40_reading_is_none_for_legacy_advertisement() -> None:
    """Legacy advertisements carry no dynamic block at all."""
    legacy = bytes.fromhex("0236006c00c301") + b"\x6e\x0f" + b"\x16" + b"\x01"

    assert parse_advertisement(legacy).sht40_reading() is None


def test_chip_temperature_is_not_the_sensor_reading() -> None:
    """The advertisement's temperature_c is the MCU's, distinct from the SHT40's."""
    payload = _v1_payload(_dynamic_with_sht40(bytes.fromhex("d7c109")), temperature_c=31.0)
    adv = parse_advertisement(payload)

    reading = adv.sht40_reading()
    assert reading is not None
    assert adv.temperature_c == pytest.approx(31.0)
    assert reading.temperature_c == pytest.approx(22.4)

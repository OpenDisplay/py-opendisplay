"""The setup and upgrade flow: where a device stands, and the steps that take it to set up.

``device_state()`` classifies a device from what it signals; ``plan_setup()`` lists the steps. Both
are pure; a runner performs them. Ported from the website's wizard planner (Web ``od-wizard.js``).
"""

from .planner import SetupPlan, Step, plan_setup
from .state import DeviceState, device_state

__all__ = ["DeviceState", "SetupPlan", "Step", "device_state", "plan_setup"]

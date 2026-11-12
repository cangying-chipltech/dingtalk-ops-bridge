"""Test suite for dingtalk-ops-bridge."""

import os
import tempfile


os.environ["ACTION_PLUGIN_DIR"] = os.path.join(
    tempfile.gettempdir(), "dingtalk-ops-bridge-tests-no-plugins"
)

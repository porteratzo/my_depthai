# Test Suite Documentation

This directory contains the test suite for the my_depthai package.

## Running Tests

### Quick Start

Run all tests:
```bash
pytest tests/
```

Run with coverage:
```bash
pytest tests/ --cov=mydepthai --cov-report=html
```

Or use the test runner script:
```bash
bash run_tests.sh
```

### Running Specific Tests

Run tests for a specific module:
```bash
pytest tests/test_config_utils.py
```

Run a specific test class:
```bash
pytest tests/test_config_utils.py::TestWriteConfig
```

Run a specific test:
```bash
pytest tests/test_config_utils.py::TestWriteConfig::test_writes_valid_config_atomically
```

### Test Markers

Tests can be marked for selective execution:

- `@pytest.mark.unit` - Unit tests (no hardware required)
- `@pytest.mark.integration` - Integration tests
- `@pytest.mark.hardware` - Tests requiring actual OAK-D hardware (`test_hardware_smoke.py`; skips when no camera is attached)
- `@pytest.mark.slow` - Tests that take longer to run

Currently every test is `unit`.

Run only unit tests:
```bash
pytest -m unit
```

Skip slow tests:
```bash
pytest -m "not slow"
```

Skip hardware tests (when no device connected):
```bash
pytest -m "not hardware"
```

## Test Structure

```
tests/
├── conftest.py                  # import-time depthai stub; sample_frame / sample_disparity
├── test_basic_dai_camera.py     # BasicDAICamera
├── test_broker_maxlen.py        # stream maxlen (0 = unlimited)
├── test_broker_reload_rollback.py # reload_cameras rolls back on failure
├── test_broker_settings.py      # BrokerSettings
├── test_camera_broker_main.py   # camera_broker.main() CLI/config wiring
├── test_camera_broker_publish.py# CameraBroker publish/reconnect/reload (incl. settings-only)
├── test_camera_health.py        # classify_health
├── test_camera_lifecycle.py     # no leaked handles across reconnects
├── test_camera_manager.py       # CameraManager + shipped config validation
├── test_config_utils.py         # config validation / write_config
├── test_depthai_modules.py      # camera/config modules (mock SDK)
├── test_depthai_real_host.py    # same modules vs. the real SDK (subprocess)
├── test_depthai_sdk_surface.py  # SDK names/methods exist in the installed wheel
├── test_fallback_frames.py      # "CAMERA OFFLINE" frame
├── test_web_auth.py             # X-Broker-Token, Host/Origin guards, revive route
└── test_web_server.py           # FastAPI UI on :5002
```

## Writing Tests

### Test Naming Convention

- Test files: `test_*.py`
- Test classes: `Test*`
- Test functions: `test_*`

### Using Fixtures

Shared fixtures in `conftest.py` are `sample_frame` and `sample_disparity`. The `depthai` module
is replaced by a MagicMock at conftest import time; to give one test a different `dai`, patch the
module attribute rather than touching `sys.modules`:

```python
import mydepthai.depthai_controllers.basic_dai_camera as bdc_mod

def test_with_fake_sdk(monkeypatch):
    monkeypatch.setattr(bdc_mod, "dai", MyFakeDai())
```

### Mocking hardware vs. the real SDK

A MagicMock accepts any argument, so it cannot catch signature or type errors (depthai rejects
floats for `int` parameters, for example). For behavior that depends on the SDK's own types, add a
scenario to `test_depthai_real_host.py`: it runs the real wheel in a fresh interpreter, and
`CameraControl`/`MessageQueue`/`ImgFrame` need no device.

## Test Coverage

After running tests with coverage, view the report:

```bash
# Terminal report
pytest --cov=mydepthai --cov-report=term-missing

# HTML report (open in browser)
pytest --cov=mydepthai --cov-report=html
open htmlcov/index.html
```

## Continuous Integration

Tests run automatically on GitHub Actions for:
- Every push to master/main/develop branches
- Every pull request
- Multiple Python versions (3.8, 3.9, 3.10, 3.11)

See `.github/workflows/ci.yml` for CI configuration.

## Test Categories

### Unit Tests
- Test individual functions and methods
- Use mocks for hardware dependencies
- Fast execution
- No actual hardware required

### Integration Tests (Future)
- Test interaction between components
- May require hardware
- Slower execution

### Hardware Tests (Future)
- Test with actual OAK-D devices
- Require connected hardware
- Only run in specific environments

## Adding New Tests

1. Create test file following naming convention
2. Import necessary fixtures from conftest.py
3. Use appropriate test markers
4. Mock hardware dependencies
5. Write clear test names and docstrings
6. Run tests locally before committing

Example:
```python
import pytest
from unittest.mock import Mock, patch

class TestNewFeature:
    """Tests for new feature."""

    def test_basic_functionality(self):
        """Test basic functionality of new feature."""
        # Arrange
        # Act
        # Assert
        pass
```

## Troubleshooting

### Import Errors
Make sure the package is installed in development mode:
```bash
pip install -e .
```

### Mock Issues
If mocks aren't working, check the import path:
```bash
python -c "import mydepthai.depthai_sdk_controllers.oak_sdk_cap; print(oak_sdk_cap.__file__)"
```

### Coverage Not Generated
Ensure pytest-cov is installed:
```bash
pip install pytest-cov
```

## Resources

- [pytest Documentation](https://docs.pytest.org/)
- [unittest.mock Documentation](https://docs.python.org/3/library/unittest.mock.html)
- [pytest-cov Documentation](https://pytest-cov.readthedocs.io/)

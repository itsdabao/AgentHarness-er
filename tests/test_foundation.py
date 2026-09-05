import importlib


def test_package_imports_without_runtime_infrastructure() -> None:
    module = importlib.import_module("minder_harness")

    assert module.__version__ == "0.1.0"

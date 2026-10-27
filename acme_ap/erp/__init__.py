"""ERP boundary. The pipeline only talks to `ErpAdapter`; swap the mock for a real connector."""
from .base import ErpAdapter, ItemRecord, VendorRecord
from .mock_netsuite import MockNetSuite

__all__ = ["ErpAdapter", "ItemRecord", "VendorRecord", "MockNetSuite"]

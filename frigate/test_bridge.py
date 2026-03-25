import json
import unittest
from unittest.mock import MagicMock, patch

# Import the module to test
import meshtastic_frigate_bridge as bridge

class TestBridgePayload(unittest.TestCase):
    def test_handle_frigate_event(self):
        # Mock the interface
        mock_iface = MagicMock()
        bridge.iface = mock_iface
        
        # Create a sample event
        test_event = {
            "camera": "front_door",
            "label": "person",
            "timestamp": 1690000000,
            "snapshot_url": "http://localhost:5000/api/events/123/snapshot.jpg",
            "type": "new",
            "has_snapshot": True
        }
        
        # Process the event
        bridge.handle_frigate_event(test_event)
        
        # Verify that sendData was called once
        mock_iface.sendData.assert_called_once()
        
        # Get the arguments passed to sendData
        args, kwargs = mock_iface.sendData.call_args
        payload_bytes = args[0]
        
        # Decode and verify the payload
        payload_dict = json.loads(payload_bytes.decode())
        
        self.assertEqual(payload_dict["camera"], "front_door")
        self.assertEqual(payload_dict["label"], "person")
        self.assertEqual(payload_dict["time"], 1690000000)

if __name__ == "__main__":
    unittest.main()

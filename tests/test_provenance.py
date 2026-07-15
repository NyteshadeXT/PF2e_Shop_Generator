import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import provenance


class ProvenanceTests(unittest.TestCase):
    def test_fingerprint_tracks_generation_service_instead_of_web_routes(self):
        generation_path = provenance.PROJECT_ROOT / "services" / "generation.py"
        app_path = provenance.PROJECT_ROOT / "app.py"

        self.assertIn(generation_path, provenance._GENERATION_CODE)
        self.assertNotIn(app_path, provenance._GENERATION_CODE)

    def test_fingerprint_is_stable_until_an_input_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "catalog.db"
            source.write_bytes(b"first catalog")
            provenance._fingerprint_for_signature.cache_clear()
            with patch.object(provenance, "_source_paths", return_value=(source,)):
                first = provenance.generation_fingerprint()
                repeated = provenance.generation_fingerprint()
                source.write_bytes(b"second catalog with changed content")
                changed = provenance.generation_fingerprint()

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, changed)
        self.assertEqual(len(first), 16)


if __name__ == "__main__":
    unittest.main()

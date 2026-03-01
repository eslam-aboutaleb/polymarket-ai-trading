import unittest

from app.services.leaderboard_service import _parse_next_data


class LeaderboardParsingTests(unittest.TestCase):
    def test_parse_next_data_valid_payload(self):
        html = """
        <html>
          <head>
            <script id="__NEXT_DATA__" type="application/json">
              {"props":{"pageProps":{"ok":true}}}
            </script>
          </head>
        </html>
        """
        parsed = _parse_next_data(html)
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed["props"]["pageProps"]["ok"], True)

    def test_parse_next_data_missing_script(self):
        html = "<html><head></head><body>No next data here.</body></html>"
        self.assertIsNone(_parse_next_data(html))

    def test_parse_next_data_malformed_json(self):
        html = """
        <script id="__NEXT_DATA__" type="application/json">
          {"props":{"pageProps":{"broken":true}
        </script>
        """
        self.assertIsNone(_parse_next_data(html))


if __name__ == "__main__":
    unittest.main()

import random
import unittest

from zeuscast import protocol as p


class FramingTests(unittest.TestCase):
    def test_conn_frame_matches_hand_computed_bytes(self):
        payload = b"POST conn\r\n\r\n"
        # LEN = 18 (0x12); checksum = (0x00 + 0x12 + sum(payload)) & 0xFF = 0x54
        self.assertEqual(p.encode_frame(payload), b"\x5a\x00\x12" + payload + b"\x54\x5a")

    def test_delimiter_and_escape_bytes_are_escaped(self):
        # "Z" is 0x5A and "[" is 0x5B; LEN = 7, checksum = 7 + 0x5A + 0x5B = 0xBC
        self.assertEqual(p.encode_frame("Z["), bytes.fromhex("5a 00 07 5b 01 5b 02 bc 5a"))

    def test_round_trip_random_payloads(self):
        rng = random.Random(1)
        for _ in range(500):
            text = "".join(chr(rng.randint(32, 126)) for _ in range(rng.randint(0, 300)))
            response = p.decode_frame(p.encode_frame(text))
            self.assertEqual(response.text, text)
            self.assertTrue(response.valid_checksum)

    def test_corrupted_checksum_is_flagged(self):
        frame = bytearray(p.encode_frame("200\r\n"))
        frame[-2] ^= 0x01
        self.assertFalse(p.decode_frame(bytes(frame)).valid_checksum)

    def test_unescape_leaves_unknown_sequences(self):
        self.assertEqual(p.unescape(b"\x5b\x03\x5b"), b"\x5b\x03\x5b")


class FrameReaderTests(unittest.TestCase):
    def test_byte_by_byte_and_back_to_back_frames(self):
        stream = p.encode_frame("200\r\n{\"a\":1}") + p.encode_frame("200\r\n")
        reader = p.FrameReader()
        responses = []
        for b in stream:
            responses += reader.feed(bytes([b]))
        self.assertEqual([r.text for r in responses], ["200\r\n{\"a\":1}", "200\r\n"])
        self.assertEqual(responses[0].body, {"a": 1})

    def test_leading_garbage_and_stray_delimiter(self):
        reader = p.FrameReader()
        responses = reader.feed(b"noise\x5a" + p.encode_frame("200\r\n"))
        self.assertEqual([r.text for r in responses], ["200\r\n"])

    def test_partial_frame_is_kept(self):
        reader = p.FrameReader()
        frame = p.encode_frame("200\r\n")
        self.assertEqual(reader.feed(frame[:4]), [])
        self.assertEqual(reader.pending(), frame[:4])
        self.assertEqual(len(reader.feed(frame[4:])), 1)


class RequestTests(unittest.TestCase):
    def test_body_request_matches_zeus_cast_format(self):
        self.assertEqual(
            p.build_request("POST brightness 1", {"value": 80}),
            'POST brightness 1\r\nContentType=json\r\nContentLength=12\r\n\r\n{"value":80}',
        )

    def test_sequence_number_header(self):
        self.assertEqual(
            p.build_request("POST rotate 1", {"degree": 90}, seq=101),
            'POST rotate 1\r\nSeqNumber=101\r\nContentType=json\r\nContentLength=13\r\n\r\n{"degree":90}',
        )

    def test_bodyless_request(self):
        self.assertEqual(p.build_request("POST conn"), "POST conn\r\n\r\n")

    def test_booleans_are_lowercase_json(self):
        self.assertTrue(p.build_request("POST osdState 1", {"enable": False}).endswith('{"enable":false}'))


class ResponseTests(unittest.TestCase):
    def test_success_with_trailing_nul(self):
        response = p.Response.from_payload(b'200\r\n{"state":"success","blockMaxSize":4096}\x00')
        self.assertTrue(response.succeeded)
        self.assertEqual(response.body["blockMaxSize"], 4096)

    def test_failure_state(self):
        self.assertFalse(p.Response.from_payload(b'200\r\n{"state":"failure"}').succeeded)

    def test_non_200(self):
        self.assertFalse(p.Response.from_payload(b"500\r\n").ok)

    def test_version_gate(self):
        self.assertFalse(p.uses_sequence_numbers("V1.0.9"))
        self.assertTrue(p.uses_sequence_numbers("V1.0.10"))
        self.assertTrue(p.uses_sequence_numbers("V1.2.0"))
        self.assertFalse(p.uses_sequence_numbers(""))


if __name__ == "__main__":
    unittest.main()

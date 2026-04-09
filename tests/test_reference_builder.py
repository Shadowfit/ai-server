import unittest

from app.core.reference_builder import _resample_angle_sequence, _average_sequences


class ReferenceBuilderTests(unittest.TestCase):
    def test_resample_angle_sequence_to_target_length(self) -> None:
        source = [
            [170.0, 175.0, 168.0, 170.0],
            [130.0, 145.0, 120.0, 122.0],
            [90.0, 105.0, 88.0, 90.0],
        ]

        normalized = _resample_angle_sequence(source, 5)

        self.assertEqual(len(normalized), 5)
        self.assertEqual(len(normalized[0]), 4)
        self.assertAlmostEqual(normalized[0][0], 170.0)
        self.assertAlmostEqual(normalized[-1][0], 90.0)

    def test_average_sequences(self) -> None:
        first = [[100.0, 120.0], [90.0, 110.0]]
        second = [[80.0, 100.0], [70.0, 90.0]]

        averaged = _average_sequences([first, second])

        self.assertEqual(averaged, [[90.0, 110.0], [80.0, 100.0]])


if __name__ == "__main__":
    unittest.main()

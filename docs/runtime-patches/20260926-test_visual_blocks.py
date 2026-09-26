"""価格図のラベルが近い値でも重ならないこと（共有レンダラー human-first-docs）。"""
import re
import sys
import unittest

sys.path.insert(0, '/Users/laa/.agents/skills/human-first-docs/scripts')
import visual_blocks  # noqa: E402


def _fig(values):
    return {'type': 'price_map', 'title': '価格', 'unit': 'USD/oz',
            'items': [{'label': f'L{i}', 'value': v, 'display': f'{v:,.2f}'} for i, v in enumerate(values)]}


class PriceMapLayout(unittest.TestCase):
    def test_close_values_get_separated_labels_and_true_dots(self):
        svg = visual_blocks.price_map(_fig([4330, 4350, 4300, 4377.2, 4400, 4251, 4458.8]), 'figure-0')
        grounds = sorted(float(y) for y in re.findall(r'<rect class="label-ground" x="132" y="([-\d.]+)"', svg))
        self.assertEqual(len(grounds), 7)
        self.assertTrue(all(b - a >= 28 for a, b in zip(grounds, grounds[1:])))
        self.assertEqual(svg.count('leader-line'), 7)
        height = float(re.search(r'viewBox="0 0 420 ([\d.]+)"', svg)[1])
        self.assertLessEqual(grounds[-1] + 28, height)
        dots = [float(y) for y in re.findall(r'class="value-dot" cx="(?:87|80)" cy="([-\d.]+)"', svg)]
        self.assertEqual(dots, sorted(dots))  # highest price at top, positions unchanged

    def test_spread_values_keep_original_layout(self):
        svg = visual_blocks.price_map(_fig([4000, 4250, 4500]), 'figure-0')
        self.assertNotIn('leader-line', svg)
        self.assertIn('x2="403"', svg)

    def test_spread_labels_centres_groups(self):
        rows = visual_blocks.spread_labels([100, 105, 110, 300], top=24, gap=32)
        self.assertEqual(rows, [73.0, 105.0, 137.0, 300.0])
        self.assertEqual(visual_blocks.spread_labels([10, 12], top=24, gap=32), [24, 56])


if __name__ == '__main__':
    unittest.main()

import sys
from chart_viewer.config import ViewerConfig
from chart_viewer.coords.x_axis import XAxisTransform
from chart_viewer.ui.pane import ChartPane
from chart_viewer.models.entities import Bar
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)

cfg = ViewerConfig()
x_trans = XAxisTransform(config=cfg)
pane = ChartPane("main", x_trans, cfg, is_main=True)
pane.resize(800, 600)

bars = []
for i in range(200):
    # Bars 0-100: low=10, high=20
    # Bars 100-200: low=100, high=120
    low = 10.0 if i < 100 else 100.0
    high = 20.0 if i < 100 else 120.0
    bars.append(Bar(t_open=i*60, open=low+1, high=high, low=low, close=high-1, volume=100))

pane.set_data(bars, {})

# Zoom in to the rightmost part (bars 180-199)
x_trans.set_viewport_width(800 - 70)
x_trans.right_index = 199
x_trans.candle_width_px = 50.0  # (730 - future_margin) / 50 = ~13 bars visible

pane.y_trans.viewport_height_px = 600
pane.update_y_range()

print(f"p_min={pane.y_trans.p_min}, p_max={pane.y_trans.p_max}")
print(f"p_bottom={pane.y_trans.p_bottom}, p_top={pane.y_trans.p_top}")
for i in range(185, 190):
    y = pane.y_trans.price_to_y(bars[i].low)
    print(f"Bar {i} low={bars[i].low} -> y={y} (bottom is {pane.y_trans.viewport_height_px})")


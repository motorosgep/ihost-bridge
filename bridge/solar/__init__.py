"""Solar production from Huawei FusionSolar.

Two sources feed it. The kiosk link (a public, no-login view the plant's owner
switches on) gives the live output, today's curve and the running totals; the
optional Northbound API account adds the plant's history by day, month and
year. With the kiosk alone the bridge still builds a history, by keeping the
totals it sees from the day it starts.
"""

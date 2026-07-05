# MEXC Playwright Actions

This document describes the browser actions executed for each webhook command.

## Execution Strategy

All trade actions use **pure Playwright clicks** with `force=True`. This bypasses
Playwright's actionability check for pointer-event interception, so any MEXC
feature popup that survives `close_popovers()` cannot block a click from landing
on the correct element.

Before every action, `close_popovers()` attempts to dismiss all visible overlays
(up to 5 retries). It targets:

| Overlay type | CSS selector used |
|---|---|
| Ant Design modal | `.ant-modal-wrap`, `.ant-modal-mask` |
| Aria modal dialog | `[role="dialog"][aria-modal="true"]` |
| Ant popover (v1) | `.ant-popover` — covers `GuidePopupModal` |
| Ant popover (v5) | `.ant-popover-v5` — covers `AiTabPopover` |
| MEXC guide widget | `[class*="handleWrapper"]` — covers `SpotEarnGuide` |
| Other guide popups | `[class*="GuidePopup"]`, `[class*="EarnGuide"]` |

Close buttons are located by trying these selectors in order inside each overlay:

```
[class*="closeIconWrap"]   ← AiTabPopover_closeIconWrap
[class*="closeIcon"]       ← GuidePopupModal_closeIcon
[class*="CloseIcon"]
[class*="handleClose"]     ← SpotEarnGuide close button
[class*="close-btn"]
[class*="closeBtn"]
button[aria-label="Close"]
.ant-modal-close / .ant-modal-close-x
.ant-modal-footer .ant-btn-primary / .ant-btn
.ant-modal-body .ant-btn-primary
.ant-modal-content .ant-btn-primary
.ant-popover-inner-content .ant-btn
.ant-popover-v5-inner-content .ant-btn
```

If no button is found for a remaining overlay, `Escape` is pressed as a last resort.

---

### Selector resilience (updated Apr 2026)

```python
# 1. Dismiss overlays
close_popovers()

# 2. Click the Open tab
page.get_by_test_id("contract-trade-order-form-tab-open").click(force=True)

# 3. Click the Market tab
page.get_by_test_id("contract-trade-order-form").get_by_role("tab", name="Market").click(force=True)

# 4. Click the 100% quantity slider step mark
page.locator(".ant-slider-v2-step > span:nth-child(5)").first.click(force=True)

# 5. Click Open Long
page.get_by_test_id("contract-trade-open-long-btn").click(force=True)
```

## Open Short Position

Identical flow to Open Long, with the final click targeting the short button:

```python
page.get_by_test_id("contract-trade-open-short-btn").click(force=True)
```

## Close Long Position

```python
# 1. Dismiss overlays
close_popovers()

# 2. Click the Close tab
page.get_by_test_id("contract-trade-order-form-tab-close").first.click()

# 3. Click the Market tab (second occurrence — close form)
page.get_by_test_id("contract-trade-order-form").get_by_text("Market").nth(1).click()

# 4. Click the 100% quantity slider step mark
page.locator("div:nth-child(3) > div:nth-child(2) > .ant-slider-v2 > .ant-slider-v2-step > span:nth-child(5)").click()

# 5. Click Close Long
page.get_by_test_id("contract-trade-close-long-btn").click()
```

## Close Short Position

Identical flow to Close Long, with the final click targeting the short button:

```python
page.get_by_test_id("contract-trade-close-short-btn").click()
```

## Close Position (Legacy)

```python
page.get_by_test_id("contract-trade-order-form-tab-close").first.click()
page.locator("#mexc_contract_v_close_position").get_by_text("Market").click()
page.get_by_test_id("contract-trade-close-position-btn").click()
```

---

## Debug Mode

Start the server with `--debug` to enable diagnostic output on every open-position action:

```bash
python webhook_server.py --instance 1 --port 8001 --debug
```

### What gets logged

Before any clicks, a **form state snapshot** is written to the server log:

```
Form state [open_long_start]: {
  'openTab': True,
  'openLongBtn': True,
  'openShortBtn': True,
  'sliderSpanCount': 10,
  'sliderNth5': True,
  'sliderNth5Visible': True,
  'visibleOverlays': []
}
```

| Field | Healthy value | If unhealthy |
|---|---|---|
| `sliderSpanCount` | ≥ 5 | If < 5, `span:nth-child(5)` doesn't exist — slider structure changed |
| `sliderNth5` | `True` | Element missing from DOM |
| `sliderNth5Visible` | `True` | Element present but hidden — may need a tab click first |
| `visibleOverlays` | `[]` (empty) | Any entry means an overlay survived `close_popovers()` and may still intercept clicks |

### Screenshots saved

Four screenshots are saved to the project root after each step:

| Filename | Captured after |
|---|---|
| `debug_open_long_1_start_<ts>.png` | `close_popovers()` completes |
| `debug_open_long_2_after_tabs_<ts>.png` | Open + Market tab clicks |
| `debug_open_long_3_after_slider_<ts>.png` | Quantity slider click |
| `debug_open_long_4_after_button_<ts>.png` | Open Long button click |
| `debug_open_long_error_<ts>.png` | Only if an exception is thrown |

`open_short` produces equivalent files with the `open_short_` prefix.

### Adding a new overlay type

If `close_popovers` logs `Blocking modal/popover may still be visible after retries`
and the `visibleOverlays` field shows an unrecognised class:

1. Inspect the element's HTML in the browser
2. Add its root container class (e.g. `[class*="NewWidget"]`) to both the
   `candidates` and `stillVisible` selector lists in `close_popovers()`
3. Add its close button's partial class to the `closeSelectors` list

---

## Webhook Payload Reference

### open_long / open_short

```json
{
    "action": "open_long",
    "symbol": "SOL_USDT",
    "leverage": 1,
    "quantity": 100
}
```

### close_long / close_short

```json
{
    "action": "close_long",
    "symbol": "SOL_USDT",
    "quantity": 100
}
```

### close_position (legacy)

```json
{
    "action": "close_position",
    "symbol": "SOL_USDT"
}
```

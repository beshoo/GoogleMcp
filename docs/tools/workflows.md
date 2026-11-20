# Workflows

High-level tools that do several API calls in one step, plus the edit-commit
safety net used by publishing tools.

---

## Regional pricing

Google Play wants a price for every billable region. These tools take one
price and use `monetization.convertRegionPrices`, so the result matches what
the Play Console does when you set a default price: Play's exchange rates and
local price endings. The product is created at the regions version the
conversion returned.

### convert_region_prices

Preview the conversion. Read-only.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `package_name` | string | Yes | — | App package name |
| `price` | string | Yes | — | Base price in major units, e.g. `"4.99"` |
| `currency_code` | string | No | `USD` | ISO 4217 currency of `price` |

Returns: `base_price`, `regions_version`, `region_count`, `regional_prices`
(`{"DE": "5.49 EUR", ...}`), `other_regions_usd`, `other_regions_eur`.

### create_one_time_product_with_price

Create a one-time in-app product with one legacy-compatible buy option priced
in every region, then activate it.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `package_name` | string | Yes | — | App package name |
| `product_id` | string | Yes | — | Lowercase letters, digits, `_`, `.` |
| `title` | string | Yes | — | Title in `language` |
| `description` | string | Yes | — | Description in `language` |
| `price` | string | Yes | — | e.g. `"0.99"` |
| `currency_code` | string | No | `USD` | Currency of `price` |
| `language` | string | No | `en-US` | Listing language |
| `extra_listings` | object | No | — | `{"de-DE": {"title": "...", "description": "..."}}` |
| `purchase_option_id` | string | No | `default` | Purchase option ID |
| `activate` | bool | No | `true` | Activate the purchase option |
| `overwrite` | bool | No | `false` | Replace an existing product with the same ID |

```python
create_one_time_product_with_price(
    "com.example.myapp", "coins_100", "100 Coins", "A pile of coins", "0.99",
    extra_listings={"de-DE": {"title": "100 Münzen", "description": "Ein Haufen Münzen"}},
)
```

### batch_create_one_time_products_with_price

Same as above for a list of products (`product_id`, `title`, `description`,
`price`, and optional `currency_code`, `language`, `extra_listings`,
`purchase_option_id`). Each distinct price is converted once. A failing
product does not stop the rest.

### create_subscription_with_price

Create an auto-renewing subscription with one base plan priced in every
region, then activate the base plan. Add trials or intro prices afterwards
with `create_subscription_offer`.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `package_name` | string | Yes | — | App package name |
| `product_id` | string | Yes | — | Subscription ID |
| `title` | string | Yes | — | Title in `language` |
| `price` | string | Yes | — | Price per billing period |
| `base_plan_id` | string | No | `monthly` | Lowercase letters, digits, `-` |
| `billing_period` | string | No | `P1M` | `P1W`, `P1M`, `P3M`, `P6M`, `P1Y` |
| `currency_code` | string | No | `USD` | Currency of `price` |
| `language` | string | No | `en-US` | Listing language |
| `description` | string | No | — | Max 200 characters |
| `benefits` | list | No | — | Up to 4 benefits |
| `extra_listings` | object | No | — | Translations (`title`, `description`, `benefits`) |
| `grace_period` | string | No | — | `P3D`, `P7D`, `P14D`, `P30D` |
| `activate` | bool | No | `true` | Activate the base plan |
| `free_trial` | string | No | — | Free period before the first charge, e.g. `P7D`, `P1M` |
| `trial_eligibility` | string | No | `new_customers` | `new_customers`, `new_customers_any_subscription`, `developer_determined` |

If activation or the free trial fails after the subscription was created, the
result is still `success: true` with `activated: false` and the follow-up step
in `warnings`, because the subscription exists and a plain retry would fail.

```python
# 1 month free, then $4.99 every month
create_subscription_with_price(
    "com.example.myapp", "premium", "Premium", "4.99", free_trial="P1M",
    benefits=["No ads", "Cloud sync"],
)
```

### create_free_trial_offer

Add a free trial to an existing base plan: free for `trial_duration`, then the
base plan's normal price every period. The offer is enabled in every region
the base plan is sold in.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `package_name` | string | Yes | — | App package name |
| `product_id` | string | Yes | — | Subscription ID |
| `base_plan_id` | string | Yes | — | Base plan to extend (must be active to activate the offer) |
| `trial_duration` | string | No | `P1M` | ISO 8601 period: `P3D`, `P7D`, `P2W`, `P1M`, ... |
| `offer_id` | string | No | `free-trial` | Offer ID |
| `eligibility` | string | No | `new_customers` | `new_customers` (never had this subscription), `new_customers_any_subscription` (never had any subscription in the app), `developer_determined` (only when the app requests the offer) |
| `activate` | bool | No | `true` | Activate the offer |

---

## Images and listings

### replace_listing_images

Upload several images for one language and image type in a single edit and
commit. By default the existing images of that type are removed first, so the
listing ends up with exactly these images in this order. If any upload fails
nothing changes.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `package_name` | string | Yes | — | App package name |
| `language` | string | Yes | — | e.g. `en-US` |
| `image_type` | string | Yes | — | `phoneScreenshots`, `sevenInchScreenshots`, `tenInchScreenshots`, `tvScreenshots`, `wearScreenshots` (up to 8), `icon`, `featureGraphic`, `tvBanner` (exactly 1) |
| `image_paths` | list | Yes | — | Local PNG/JPEG paths in display order |
| `replace_existing` | bool | No | `true` | `false` appends instead |

### sync_listing_from_directory

Sync listing text and images for every language from a folder in the fastlane
`supply` layout:

```text
<directory>/<lang>/title.txt | short_description.txt | full_description.txt | video.txt
<directory>/<lang>/images/icon.png | featureGraphic.png | tvBanner.png
<directory>/<lang>/images/phoneScreenshots/*.png  (and the other screenshot types)
```

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `package_name` | string | Yes | — | App package name |
| `directory` | string | Yes | — | Metadata folder |
| `languages` | list | No | all | Only sync these language folders |
| `replace_images` | bool | No | `true` | Make Play's images match the folder exactly |
| `dry_run` | bool | No | `false` | Validate and report without changing anything (allowed in read-only mode) |

Only what exists in the folder is changed. Empty text files are skipped (and
listed in `warnings`), unreadable or non-UTF-8 files are reported in
`problems`, and text limits and image counts are validated before anything is
sent. Everything is applied in one commit.

---

## Commit safety net

Every edit-based tool commits through the same helper:

- **Draft apps.** An app that has never been published only accepts `draft`
  releases. `deploy_app` and `promote_release` retry the release as a draft
  in the same edit, and say so in the result.
- **Changes not sent for review.** For deploys, promotions,
  `replace_listing_images` and `sync_listing_from_directory`, when Play refuses
  to send changes for review automatically the commit is retried with
  `changesNotSentForReview=true` and the result says so. The changes are
  saved; send them for review from the Play Console. Other tools (halting a
  rollout, single listing updates, ...) report the error instead, so a change
  that didn't go live is never reported as done.
- **Changes already in review.** By default Google cancels an in-progress
  review when you commit. Set `PLAY_STORE_MCP_IN_REVIEW_BEHAVIOR=error` to
  make commits fail instead, or `cancel` to keep Google's default explicitly.

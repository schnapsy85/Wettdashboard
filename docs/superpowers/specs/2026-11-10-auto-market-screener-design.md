# Auto Market Screener - Design

## Intent

Build a private dashboard page that finds real online car listings for two
vehicle profiles near Norderstedt. The page should make it fast to compare
usable offers, open the original listing, and save favorites that remain
available across devices.

## Goals

- Search broadly across public automotive sites.
- Keep only reliable sources with enough structured evidence to show a useful
  listing preview.
- Show both vehicle profiles together on one responsive page.
- Refresh the result set four times per day.
- Store favorites server-side so they are available from the dashboard anywhere.
- Remove listings once their original offer is confirmed unavailable.

## Search profiles

Both profiles use the same fixed vehicle constraints:

- Model-specific long version.
- Registration year from 2025 onward.
- Maximum 40,000 km mileage.
- Maximum 40,000 EUR price.
- Explicitly accident-free listing evidence.
- Maximum 250 km from Norderstedt.

Profiles:

1. VW Bus Multivan, long version.
2. Ford Tourneo Custom, long version.

The maximum price and maximum distance are exposed as editable global filters,
pre-filled with 40,000 EUR and 250 km. The model, year, mileage, long-version,
and accident-free requirements remain fixed search criteria for this first
version.

## Listing quality rules

A listing is displayed only when a trusted source provides enough evidence for
the preview: title/model, price, mileage, registration year, location, source
URL, and an image URL. "Accident-free" and the long body version
must be stated by the source or be unambiguously represented in its structured
data; the screener must not guess either property.

Each listing stores its canonical URL, source name, normalized fields, source
image URL, first-seen time, last-seen time, and the last successful check. The
source URL remains the link shown to the user. Duplicate offers are merged by
canonical URL and a conservative title/location fallback.

## Page behavior

The navigation gets an `Auto Screener` entry. The page contains:

- A compact filter bar for maximum distance and maximum price.
- A last-updated indicator and the next scheduled refresh.
- A VW Multivan long-version result section.
- A Ford Tourneo Custom long-version result section.
- Listing cards with image, price, mileage, registration year, location,
  distance, source, accident-free evidence, favorite control, and original
  offer link.
- Empty, stale, and source-quality states that explain why no result is shown.

Favorites are stored in the server database and keyed to the dashboard user.
The Auto Screener must not add a separate password prompt. It inherits the
dashboard's existing deployment/network access boundary and keeps CSRF
protection for favorite changes. A general dashboard login remains a separate
security project rather than being invented for this page.

## Refresh and availability

Run the discovery and re-check job four times per day in the dashboard's local
Europe/Berlin timezone. A sensible initial schedule is 00:00, 06:00, 12:00,
and 18:00 local time.

The job re-checks known listings, discovers new ones, normalizes qualifying
records, and updates the database. A listing is removed from active results
after the original offer is confirmed gone (for example, a definitive not-found
response), while transient source failures are recorded without deleting
otherwise valid data.

## Implementation approach

Use the selected automatic hybrid approach:

- A local scheduled worker performs a broad web-search/discovery step four
  times daily; it does not depend on a chat turn being active.
- A small source registry allows only known reliable sources to enter the
  active result set.
- Source adapters parse each approved source into one normalized listing shape.
- The existing Python dashboard backend and SQLite are extended for listings,
  search settings, refresh runs, and favorites.
- A system scheduler invokes the refresh job four times daily.
- The existing plain HTML/CSS dashboard style is extended with one new page;
  no frontend framework or new dependency is needed.

The worker uses a web-search adapter for discovery and then fetches the public
listing pages itself. The adapter is a technical provider boundary, not a user
password: its credentials, if required by the chosen search service, live only
in the service environment. This balances coverage with data quality. Directly
scraping every automotive site would be broader on paper but would be brittle
and would make reliable field extraction difficult to maintain.

## Non-goals

- Buying, reserving, contacting, or bidding on a vehicle.
- Scraping login-only or paywalled listings.
- Guaranteeing every public listing on the web is found.
- Price-history charts, alerts, financing calculations, or recommendations.
- A multi-user marketplace or sharing system.

## Anti-goals

- Showing a false positive because a model name merely appears in the text.
- Inferring accident-free status when the source does not explicitly support it.
- Keeping broken or deleted offers in the active result list indefinitely.
- Adding a large scraping framework or frontend stack for this focused page.

## Builder calls

- The active result set is the source of truth; deleted listings disappear after
  confirmed unavailability.
- The first version uses the existing backend and SQLite rather than adding a
  hosted database or framework.
- The Auto Screener adds no separate user password; access follows the existing
  dashboard deployment boundary.
- The initial refresh schedule uses four evenly spaced local times and can be
  changed in configuration later.

## Verification

Leave one runnable self-check covering normalization and rejection of listings
missing required evidence. Verify the page manually at desktop and mobile
widths, and verify that a favorite survives a new browser session against the
same dashboard backend.

## Status

Approved direction for the next revision: remove the Auto Screener password
flow and make the scheduled worker the primary discovery path. Live results
remain unavailable until the web-search adapter is connected; the page fails
closed and shows no invented listings without it.

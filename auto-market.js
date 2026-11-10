const $ = selector => document.querySelector(selector);
const appState = { csrf: '', filters: {max_price_eur: 40000, max_distance_km: 250}, listings: [], profiles: [] };

async function api(url, options = {}) {
  const response = await fetch(url, {credentials: 'same-origin', ...options});
  const contentType = response.headers.get('Content-Type') || '';
  const data = contentType.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) {
    const error = new Error(data && data.error ? data.error : 'Anfrage fehlgeschlagen');
    error.status = response.status;
    throw error;
  }
  return data;
}

function euro(value) {
  return Number(value).toLocaleString('de-DE', {style: 'currency', currency: 'EUR', maximumFractionDigits: 0});
}

function number(value, suffix = '') {
  return Number(value).toLocaleString('de-DE', {maximumFractionDigits: 1}) + suffix;
}

function text(value) {
  return value == null || value === '' ? 'UNAVAILABLE' : String(value);
}

function addFact(dl, label, value) {
  const dt = document.createElement('dt');
  const dd = document.createElement('dd');
  dt.textContent = label;
  dd.textContent = value;
  dl.append(dt, dd);
}

function listingCard(listing) {
  const card = document.createElement('article');
  card.className = 'panel listing-card';
  const image = document.createElement('img');
  image.className = 'listing-image';
  image.src = listing.image_url;
  image.alt = `Fahrzeugbild: ${text(listing.title)}`;
  image.loading = 'lazy';
  image.referrerPolicy = 'no-referrer';
  image.onerror = () => {
    const fallback = document.createElement('div');
    fallback.className = 'image-fallback';
    fallback.textContent = 'Bild nicht verfuegbar';
    image.replaceWith(fallback);
  };
  card.append(image);

  const content = document.createElement('div');
  content.className = 'listing-content';
  const heading = document.createElement('div');
  heading.className = 'listing-title';
  const title = document.createElement('h3');
  title.textContent = text(listing.title);
  const favorite = document.createElement('button');
  favorite.className = 'favorite';
  favorite.type = 'button';
  favorite.textContent = listing.favorite ? 'Favorit' : 'Merken';
  favorite.setAttribute('aria-pressed', String(Boolean(listing.favorite)));
  favorite.setAttribute('aria-label', `${listing.favorite ? 'Favorit entfernen' : 'Als Favorit markieren'}: ${text(listing.title)}`);
  favorite.addEventListener('click', async () => {
    favorite.disabled = true;
    try {
      const result = await api('/api/auto-market/favorite', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': appState.csrf},
        body: JSON.stringify({listing_id: listing.id, favorite: !listing.favorite}),
      });
      listing.favorite = result.favorite;
      renderProfiles();
    } catch (error) {
      favorite.disabled = false;
      favorite.textContent = error.message;
    }
  });
  heading.append(title, favorite);
  content.append(heading);

  const price = document.createElement('div');
  price.className = 'price';
  price.textContent = euro(listing.price_eur);
  content.append(price);

  const facts = document.createElement('dl');
  facts.className = 'facts';
  addFact(facts, 'Baujahr', text(listing.year));
  addFact(facts, 'Kilometer', number(listing.mileage_km, ' km'));
  addFact(facts, 'Entfernung', number(listing.distance_km, ' km'));
  addFact(facts, 'Standort', text(listing.location));
  content.append(facts);

  const evidence = document.createElement('div');
  evidence.className = 'evidence';
  evidence.textContent = `Unfallfrei: ${text(listing.accident_free_evidence)} · Variante: ${text(listing.long_version_evidence)}`;
  content.append(evidence);

  const link = document.createElement('a');
  link.className = 'offer-link';
  link.href = listing.url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.textContent = `Originalangebot bei ${text(listing.source)} öffnen`;
  content.append(link);
  card.append(content);
  return card;
}

function renderProfiles() {
  const container = $('#profiles');
  container.replaceChildren();
  appState.profiles.forEach(profile => {
    const section = document.createElement('section');
    section.className = 'profile-section';
    section.setAttribute('aria-labelledby', `${profile.id}-title`);
    const heading = document.createElement('div');
    heading.className = 'profile-heading';
    const title = document.createElement('h2');
    title.id = `${profile.id}-title`;
    title.textContent = profile.label;
    const listings = appState.listings.filter(listing => listing.profile === profile.id);
    const count = document.createElement('span');
    count.className = 'count';
    count.textContent = `${listings.length} Treffer`;
    heading.append(title, count);
    section.append(heading);
    if (!listings.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'Keine gepruefte Anzeige mit den aktuellen Filtern.';
      section.append(empty);
    } else {
      const cards = document.createElement('div');
      cards.className = 'cards';
      listings.forEach(listing => cards.append(listingCard(listing)));
      section.append(cards);
    }
    container.append(section);
  });
}

function renderState(state) {
  Object.assign(appState, state);
  $('#max-price').value = state.filters.max_price_eur;
  $('#max-distance').value = state.filters.max_distance_km;
  const refresh = state.last_refresh;
  $('#refresh-state').textContent = refresh
    ? `Letzte Pruefung: ${text(refresh.finished_at || refresh.started_at)} · Naechster Lauf: ${text(state.next_refresh)}`
    : `Noch keine Pruefung · Naechster Lauf: ${text(state.next_refresh)}`;
  renderProfiles();
}

async function loadState() {
  const params = new URLSearchParams({
    max_price_eur: String(appState.filters.max_price_eur),
    max_distance_km: String(appState.filters.max_distance_km),
  });
  try {
    renderState(await api('/api/auto-market/state?' + params));
  } catch (error) {
    $('#refresh-state').textContent = error.message;
  }
}

$('#filter-form').addEventListener('submit', event => {
  event.preventDefault();
  appState.filters = {
    max_price_eur: Number($('#max-price').value),
    max_distance_km: Number($('#max-distance').value),
  };
  loadState();
});

loadState();
window.setInterval(loadState, 300000);

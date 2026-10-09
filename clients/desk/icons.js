// Stroke icons: 24 viewBox, currentColor, 1.8 stroke, round joins. All drawn for this desk.
const svg = (body, width = 1.8) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${width}" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;

window.ICO = {
  pulse: svg('<path d="M2 12h4l2.5-6 4 12 3-9 2 3H22"/>'),
  chart: svg('<path d="M4 20h16"/><rect x="5" y="11" width="3" height="6" rx="0.8"/><rect x="10.5" y="5" width="3" height="12" rx="0.8"/><rect x="16" y="8.5" width="3" height="8.5" rx="0.8"/>'),
  scale: svg('<path d="M12 4v16M7 20h10"/><path d="M5 8h14"/><path d="M5 8l-2.5 6a2.5 2.5 0 0 0 5 0zM19 8l-2.5 6a2.5 2.5 0 0 0 5 0z"/>'),
  book: svg('<path d="M3 5.5C6 4 9 4 12 6c3-2 6-2 9-.5V19c-3-1.5-6-1.5-9 .5-3-2-6-2-9-.5z"/><path d="M12 6v13.5"/>'),
  shield: svg('<path d="M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/>'),
  left: svg('<path d="M15 5.5L8.5 12l6.5 6.5"/>', 2),
  right: svg('<path d="M9 5.5l6.5 6.5L9 18.5"/>', 2),
  chevron: svg('<path d="M9 6l6 6-6 6"/>', 2),
  hold: svg('<circle cx="12" cy="12" r="8"/><path d="M9 12h6"/>'),
  up: svg('<path d="M4 16l6-6 4 4 6-6"/><path d="M15 8h5v5"/>'),
  down: svg('<path d="M4 8l6 6 4-4 6 6"/><path d="M15 16h5v-5"/>'),
  alert: svg('<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5h.01"/>'),
  note: svg('<path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5M9 13h7M9 17h5"/>'),
  spark: svg('<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6"/>'),
  pause: svg('<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>'),
  play: svg('<path d="M7 4.5v15l12-7.5z"/>'),
  chip: svg('<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9.5 9.5h5v5h-5z"/><path d="M9 2.5V6M15 2.5V6M9 18v3.5M15 18v3.5M2.5 9H6M2.5 15H6M18 9h3.5M18 15h3.5"/>'),
  gear: svg('<circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/>'),
  order: svg('<path d="M4 7h16M4 12h10M4 17h7"/><path d="M17 14l3 3-3 3"/>'),
};

const centerX = rect => (rect.left + rect.right) / 2;
const centerY = rect => (rect.top + rect.bottom) / 2;
export const containsPoint = (rect, point, padding = 0) => rect.width > 0 && rect.height > 0
  && point.x >= rect.left - padding && point.x <= rect.right + padding
  && point.y >= rect.top - padding && point.y <= rect.bottom + padding;

/** Layout slots, not moving card identities, determine the preview index. */
export function gridSlotAt(slots, point, previousIndex = -1, hysteresis = 12) {
  if (!slots.length) return -1;
  const rows = [];
  slots.forEach((rect, index) => {
    let row = rows.find(item => Math.abs(item.top - rect.top) < Math.min(20, rect.height / 4));
    if (!row) { row = { top: rect.top, bottom: rect.bottom, entries: [] }; rows.push(row); }
    row.bottom = Math.max(row.bottom, rect.bottom); row.entries.push({ rect, index });
  });
  rows.sort((a, b) => a.top - b.top);
  rows.forEach(row => row.entries.sort((a, b) => a.rect.left - b.rect.left));
  const rowCenter = row => (row.top + row.bottom) / 2;
  let rowIndex = rows.findIndex((row, index) => index === rows.length - 1
    || point.y < (rowCenter(row) + rowCenter(rows[index + 1])) / 2);
  const previousRow = rows.findIndex(row => row.entries.some(entry => entry.index === previousIndex));
  if (previousRow >= 0 && rowIndex !== previousRow) {
    const boundary = (rowCenter(rows[previousRow]) + rowCenter(rows[rowIndex])) / 2;
    if (rowIndex > previousRow ? point.y < boundary + hysteresis : point.y > boundary - hysteresis) rowIndex = previousRow;
  }
  const row = rows[rowIndex];
  let column = row.entries.findIndex((entry, index) => index === row.entries.length - 1
    || point.x < (centerX(entry.rect) + centerX(row.entries[index + 1].rect)) / 2);
  const previousColumn = row.entries.findIndex(entry => entry.index === previousIndex);
  if (previousColumn >= 0 && column !== previousColumn) {
    const boundary = (centerX(row.entries[previousColumn].rect) + centerX(row.entries[column].rect)) / 2;
    if (column > previousColumn ? point.x < boundary + hysteresis : point.x > boundary - hysteresis) column = previousColumn;
  }
  return row.entries[column].index;
}

export function moveCardToSlot(ids, sourceId, index) {
  const rest = ids.filter(id => id !== sourceId);
  rest.splice(Math.max(0, Math.min(rest.length, index)), 0, sourceId);
  return rest;
}

export function cardOrderPayload(ids, preview, sourceId) {
  if (!preview || preview.join("\0") === ids.join("\0")) return null;
  const index = preview.indexOf(sourceId);
  if (index < 0) return null;
  return { sourceId, visibleIds: ids,
    ...(index < preview.length - 1 ? { beforeId: preview[index + 1] } : { afterId: preview[index - 1] }) };
}

export function distanceToRect(rect, point) {
  return Math.hypot(Math.max(rect.left - point.x, 0, point.x - rect.right), Math.max(rect.top - point.y, 0, point.y - rect.bottom));
}

/** Prior target gets a small exit tolerance, avoiding flicker at tab edges. */
export function cardDropTarget(targets, point, previousNode = null) {
  const exact = targets.filter(target => containsPoint(target.rect, point));
  const hit = exact.sort((a, b) => a.rect.width * a.rect.height - b.rect.width * b.rect.height)[0]
    || targets.find(target => target.type !== "apiPool" && target.node === previousNode && containsPoint(target.rect, point, 6)) || null;
  const near = hit || targets.map(target => ({ ...target, distance: distanceToRect(target.rect, point) }))
    .filter(target => target.type !== "apiPool" && target.distance <= 88).sort((a, b) => a.distance - b.distance)[0] || null;
  return { hit, near };
}

/** Keep the entire thumbnail outside the target, including narrow viewports. */
export function dockCardPreview(target, width, height, viewport) {
  const scale = Math.min(.32, 108 / width, 72 / height);
  const w = width * scale, h = height * scale, gap = 14;
  let x = target.right + gap, y = centerY(target) - h / 2;
  if (x + w > viewport.width - 8) x = target.left - gap - w;
  if (x < 8) { x = Math.max(8, Math.min(viewport.width - w - 8, centerX(target) - w / 2)); y = target.bottom + gap; }
  if (y + h > viewport.height - 8 && target.top - gap - h >= 8) y = target.top - gap - h;
  return { x, y: Math.max(8, y), scale };
}

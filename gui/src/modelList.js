export function listedModels(values = []) {
  const seen = new Set();
  return (Array.isArray(values) ? values : []).flatMap(value => {
    const id = typeof value === "string" ? value.trim() : String(value?.id || value?.slug || value?.name || "").trim();
    if (!id || seen.has(id)) return [];
    seen.add(id);
    const label = typeof value === "object" ? String(value?.displayName || value?.display_name || value?.name || id) : id;
    return [{ id, label }];
  });
}

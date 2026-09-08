const PLAN_ALIASES = [
  {
    tier: "gold",
    label: "Pro 20x",
    aliases: ["pro20x", "promax", "chatgptpro20x", "chatgptpromax", "codexpro20x"],
  },
  {
    tier: "silver",
    label: "Pro 5x",
    aliases: ["pro5x", "prolite", "chatgptpro5x", "chatgptprolite", "codexpro5x"],
  },
  {
    tier: "neutral",
    label: "Pro",
    aliases: ["pro", "proplan", "chatgptpro", "chatgptproplan"],
  },
  {
    tier: "bronze",
    label: "Plus",
    aliases: ["plus", "plusplan", "chatgptplus", "chatgptplusplan"],
  },
  {
    tier: "team",
    label: "Team",
    aliases: ["team", "teamplan", "chatgptteam", "chatgptteamplan"],
  },
  {
    tier: "business",
    label: "Business",
    aliases: ["business", "businessplan", "chatgptbusiness", "chatgptbusinessplan"],
  },
  {
    tier: "enterprise",
    label: "Enterprise",
    aliases: ["enterprise", "enterpriseplan", "chatgptenterprise", "chatgptenterpriseplan"],
  },
  {
    tier: "edu",
    label: "Edu",
    aliases: ["edu", "eduplan", "education", "chatgptedu", "chatgpteduplan"],
  },
  {
    tier: "go",
    label: "Go",
    aliases: ["go", "goplan", "chatgptgo", "chatgptgoplan"],
  },
  {
    tier: "free",
    label: "Free",
    aliases: ["free", "freeplan", "chatgptfree", "chatgptfreeplan"],
  },
];

function compactPlanToken(value) {
  return String(value ?? "")
    .trim()
    .toLocaleLowerCase("en-US")
    .replace(/[^a-z0-9]+/g, "");
}

function classifyPlan(value) {
  let token = compactPlanToken(value);
  if (!token) return null;
  token = token.replace(/^((?:chatgpt|codex)?pro(?:20x|5x|max|lite))(?:monthly|annual|yearly)$/, "$1");
  return PLAN_ALIASES.find(({ aliases }) => aliases.includes(token)) ?? null;
}

/**
 * Resolve a subscription badge from highest-confidence to lowest-confidence
 * values (normally displayed label, account plan, then usage plan). Generic
 * Pro may be refined by an explicit Pro variant; concrete tiers keep order.
 */
export function subscriptionBadge(values, freeAccount = false) {
  const candidates = (Array.isArray(values) ? values : [values]).filter(
    (value) => String(value ?? "").trim(),
  );

  if (freeAccount) return { tier: "free", label: "Free" };

  for (const candidate of candidates) {
    const match = classifyPlan(candidate);
    if (match) {
      if (match.label === "Pro") {
        const explicit = candidates.map(classifyPlan).find(
          (plan) => plan?.label === "Pro 5x" || plan?.label === "Pro 20x",
        );
        if (explicit) return { tier: explicit.tier, label: explicit.label };
        return { tier: "gold", label: "Pro 20x" };
      }
      return { tier: match.tier, label: match.label };
    }
  }

  return {
    tier: "neutral",
    label: candidates.length ? String(candidates[0]).trim() : "待识别",
  };
}

export default subscriptionBadge;

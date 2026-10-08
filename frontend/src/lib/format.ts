/** Only http(s) links may reach an href: job data is third-party, so javascript: and data: URLs are dropped. */
export function safeHttpUrl(url: string | null | undefined): string | null {
  try {
    const u = new URL(url ?? "");
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

export function formatSalary(min: number | null, max: number | null, currency: string | null): string | null {
  if (!min && !max) return null;
  const k = (n: number) => `${Math.round(n / 1000)}k`;
  const sym = !currency || currency === "USD" ? "$" : `${currency} `;
  if (min && max && min !== max) return `${sym}${k(min)}–${k(max)}`;
  return `${sym}${k((max ?? min) as number)}`;
}

export function timeAgo(iso: string | null): string | null {
  if (!iso) return null;
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000);
  if (days <= 0) return "today";
  if (days === 1) return "1 day ago";
  if (days < 30) return `${days} days ago`;
  const months = Math.floor(days / 30);
  return months === 1 ? "1 month ago" : `${months} months ago`;
}

export const WORKPLACE_LABEL: Record<string, string> = { remote: "Remote", hybrid: "Hybrid", onsite: "On-site" };
export const SENIORITY_LABEL: Record<string, string> = {
  intern: "Intern", new_grad: "New grad", junior: "Junior", mid: "Mid-level", senior: "Senior", staff: "Staff+", manager: "Manager",
};
export const VERDICT_LABEL: Record<string, string> = { strong: "Strong fit", good: "Good fit", stretch: "Stretch", no: "Poor fit" };

export const HIRE_LABEL: Record<string, string> = { yes: "Recruiter: submit", maybe: "Recruiter: maybe", no: "Recruiter: pass" };

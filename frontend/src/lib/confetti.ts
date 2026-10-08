const COLORS = ["#4f46e5", "#16a34a", "#f59e0b", "#ef4444", "#06b6d4", "#ec4899"];

export function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

/** Short canvas confetti burst. No-op under prefers-reduced-motion or when canvas is unavailable. */
export function confetti(count = 80, duration = 1600): void {
  if (typeof document === "undefined" || prefersReducedMotion()) return;
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext?.("2d");
  if (!ctx) return;
  const w = (canvas.width = window.innerWidth);
  const h = (canvas.height = window.innerHeight);
  canvas.setAttribute("aria-hidden", "true");
  canvas.className = "confetti-canvas";
  document.body.appendChild(canvas);

  const pieces = Array.from({ length: count }, () => ({
    x: w / 2,
    y: h * 0.6,
    vx: (Math.random() - 0.5) * 16,
    vy: -6 - Math.random() * 10,
    size: 5 + Math.random() * 5,
    rot: Math.random() * Math.PI,
    vr: (Math.random() - 0.5) * 0.4,
    color: COLORS[Math.floor(Math.random() * COLORS.length)],
  }));
  const start = performance.now();
  const frame = (now: number) => {
    const t = Math.max(0, now - start);
    if (t > duration) {
      canvas.remove();
      return;
    }
    ctx.clearRect(0, 0, w, h);
    ctx.globalAlpha = Math.min(1, (duration - t) / 500);
    for (const p of pieces) {
      p.vy += 0.35;
      p.x += p.vx;
      p.y += p.vy;
      p.rot += p.vr;
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.rot);
      ctx.fillStyle = p.color;
      ctx.fillRect(-p.size / 2, -p.size / 4, p.size, p.size / 2);
      ctx.restore();
    }
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

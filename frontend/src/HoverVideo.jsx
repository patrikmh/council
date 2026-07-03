import { useRef } from "react";

/**
 * HoverVideo — a <video> that plays as an animation on hover and shows a static
 * first frame otherwise. Used in place of placeholder images (e.g. the codex
 * tapestry and the round-table emblem).
 *
 * The `#t=0.1` media fragment makes browsers render the frame at 0.1s as the
 * "poster" so there is a representative still when not hovering.
 */
export default function HoverVideo({ src, className, ariaLabel, draggable = false }) {
  const ref = useRef(null);

  const handleEnter = () => {
    const v = ref.current;
    if (!v) return;
    try { v.currentTime = 0; } catch (_) { /* noop */ }
    const p = v.play();
    if (p && typeof p.catch === "function") p.catch(() => {});
  };

  const handleLeave = () => {
    const v = ref.current;
    if (!v) return;
    v.pause();
    try { v.currentTime = 0; } catch (_) { /* noop */ }
  };

  return (
    <video
      ref={ref}
      className={className}
      src={`${src}#t=0.1`}
      aria-label={ariaLabel}
      aria-hidden={ariaLabel ? undefined : true}
      muted
      loop
      playsInline
      preload="metadata"
      draggable={draggable}
      onMouseEnter={handleEnter}
      onMouseLeave={handleLeave}
    />
  );
}

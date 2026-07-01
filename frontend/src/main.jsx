import React, { useEffect } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./styles.css";

function AmbientCursor() {
  // A soft radial glow follows the pointer via CSS custom properties on
  // <body>. Rate-limited to rAF so scroll/resize don't jank. The actual
  // gradient lives in styles.css so we can tune it without a rebuild.
  useEffect(() => {
    let x = window.innerWidth / 2;
    let y = window.innerHeight / 2;
    let raf = 0;
    function apply() {
      raf = 0;
      document.body.style.setProperty("--mx", `${x}px`);
      document.body.style.setProperty("--my", `${y}px`);
    }
    function move(e) {
      x = e.clientX;
      y = e.clientY;
      if (!raf) raf = requestAnimationFrame(apply);
    }
    apply();
    window.addEventListener("pointermove", move, { passive: true });
    return () => {
      window.removeEventListener("pointermove", move);
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);
  return null;
}

createRoot(document.getElementById("root")).render(
  <>
    <AmbientCursor />
    <App />
  </>,
);

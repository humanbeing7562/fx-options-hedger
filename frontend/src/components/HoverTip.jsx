// Hover explanations that show straight away.
//
// Any element with a data-tip="..." attribute gets a small box under it while the mouse
// is over it. One listener on the whole page does this, and the box is position: fixed,
// so it is never cut off by a scrolling table.

import { useEffect, useState } from "react";

export default function HoverTip() {
  const [tip, setTip] = useState(null);   // { text, x, y } or null

  useEffect(() => {
    function onOver(event) {
      const target = event.target.closest ? event.target.closest("[data-tip]") : null;
      if (target === null) {
        setTip(null);
        return;
      }
      const box = target.getBoundingClientRect();
      // Keep the box on screen: start at the label, but no closer than 290px to the right edge
      let x = box.left;
      const maxX = window.innerWidth - 290;
      if (x > maxX) x = Math.max(maxX, 8);
      setTip({ text: target.getAttribute("data-tip"), x: x, y: box.bottom + 6 });
    }

    function onScroll() {
      setTip(null);
    }

    document.addEventListener("mouseover", onOver);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mouseover", onOver);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, []);

  if (tip === null) return null;
  return (
    <div className="hover-tip" style={{ left: tip.x + "px", top: tip.y + "px" }}>
      {tip.text}
    </div>
  );
}
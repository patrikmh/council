// CodexBanner — the top of design "1A · Illuminated Codex": the folio
// masthead strip (Anno · A Council of Machines / Fol. I r), the full-width
// council tapestry image in a gilded triple frame, and the hero block —
// gothic "Rabble" wordmark with an illuminated drop-cap lead paragraph.
// Shown as the landing hero above the round table.

export default function CodexBanner() {
  return (
    <div className="codex-banner">
      <div className="codex-folio">
        <span>Anno · A Council of Machines</span>
        <span>
          Fol. I<span className="codex-folio-r">r</span>
        </span>
      </div>

      <figure className="codex-tapestry">
        <img
          src="/council.jpg"
          alt="Hic concilium mentium artificiosarum consulitat de decisione — a council of artificial minds at the round table"
          draggable={false}
        />
      </figure>

      <div className="codex-hero">
        <p className="codex-hero-lead">
          <span className="codex-dropcap" aria-hidden="true">
            S
          </span>
          ummon a council of the world's finest models. They gather at a
          single round table — six engines of thought, each bringing its own
          reasoning to bear. They frame the options, cast their ballots, and
          argue across rounds, answering for their judgments before one
          another — until a single scribe rises to write the minutes. Pose
          your question below, and watch the council convene.
        </p>
      </div>

      {/* Placeholder divider — replace frontend/public/divider-placeholder.svg */}
      <figure className="codex-divider" role="separator">
        <img
          src="/divider-placeholder.svg"
          alt="Decorative divider (placeholder)"
          draggable={false}
        />
      </figure>
    </div>
  );
}

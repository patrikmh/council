// StoryBand — the "Bayeux Chronicle" (design 1C) narrative tapestry, woven
// into the Illuminated Codex (1A). A single video (plays on hover) carries
// the scene; the three-act narrative reads beneath it as captions:
// they gather (Poll), they contend (Debate), it is decided (Verdict).

import HoverVideo from "./HoverVideo.jsx";

const ACTS = [
  {
    key: "poll",
    head: "Hic conveniunt",
    latin: "they gather",
    body: (
      <>
        <strong>Poll.</strong> One motion is laid before the table. Each mind
        frames the options and casts its lot.
      </>
    ),
  },
  {
    key: "debate",
    head: "Hic certant",
    latin: "they contend",
    body: (
      <>
        <strong>Debate.</strong> They rebut across rounds, search for proof,
        and some flip their vote before the room.
      </>
    ),
  },
  {
    key: "verdict",
    head: "Hic decernitur",
    latin: "it is decided",
    body: (
      <>
        <strong>Verdict.</strong> The tally is read, the minutes are stitched,
        and influence is entered in the ledger.
      </>
    ),
  },
];

export default function StoryBand() {
  return (
    <div className="story-band" aria-label="How the table works">
      <div className="story-band-caption">The story, stitched left to right</div>
      <figure className="story-scene-panel story-scene-panel--single">
        <HoverVideo
          src="/codex.mp4"
          className="story-scene-video"
          ariaLabel="The story of the table: they gather, they contend, it is decided"
        />
      </figure>
      <div className="story-band-grid">
        {ACTS.map((a) => (
          <section key={a.key} className={`story-scene story-scene--${a.key}`}>
            <div className="story-scene-head">
              {a.head}
              <span className="latin">{a.latin}</span>
            </div>
            <p className="story-scene-text">{a.body}</p>
          </section>
        ))}
      </div>
    </div>
  );
}

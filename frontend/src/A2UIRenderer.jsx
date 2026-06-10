// A2UI renderer. The server sends { component, props, children } trees with
// { path: "/pointer" } bindings; we resolve bindings against the surface's
// data model and map component names onto our local widget library. The
// server never sends markup — that's the A2UI contract.

import React from "react";

function resolve(value, data) {
  if (value && typeof value === "object" && typeof value.path === "string") {
    const parts = value.path.split("/").filter(Boolean);
    let cur = data;
    for (const p of parts) {
      if (cur == null) return undefined;
      cur = cur[p];
    }
    return cur;
  }
  return value;
}

function VoterChip({ voter }) {
  return (
    <span className="chip" title={voter.reasoning}>
      <span className="chip-provider">{voter.provider}</span>
      {voter.name}
    </span>
  );
}

function PollOption({ optionId, label, votes, voters, winner, tone, total }) {
  const share = total > 0 ? votes / total : 0;
  return (
    <div className={`poll-option tone-${tone} ${winner ? "is-winner" : ""}`}>
      <div className="poll-fill" style={{ transform: `scaleX(${share})` }} />
      <div className="poll-row">
        <span className="poll-label">
          <span className="poll-id">{optionId}</span>
          {label}
        </span>
        <span className="poll-tally">
          {votes} {votes === 1 ? "vote" : "votes"}
          {winner ? " · carried" : ""}
        </span>
      </div>
      {voters?.length > 0 && (
        <div className="poll-voters">
          {voters.map((v) => (
            <VoterChip key={v.name} voter={v} />
          ))}
        </div>
      )}
    </div>
  );
}

const registry = {
  Card: (p, kids) => <div className="a2ui-card">{kids}</div>,
  Eyebrow: (p) => <div className="eyebrow">{p.text}</div>,
  Heading: (p) => <h2 className="card-heading">{p.text}</h2>,
  SectionLabel: (p) =>
    p.show === false ? null : <div className="section-label">{p.text}</div>,
  Paragraph: (p) =>
    p.text ? <p className="card-paragraph">{p.text}</p> : null,
  PollOption: (p) => <PollOption {...p} />,
};

function renderNode(node, data, ctx, key) {
  if (!node) return null;
  const props = {};
  for (const [k, v] of Object.entries(node.props ?? {})) {
    props[k] = resolve(v, data);
  }
  if (node.component === "PollOption") props.total = ctx.totalVotes;
  const children = (node.children ?? []).map((child, i) =>
    renderNode(child, data, ctx, i)
  );
  const factory = registry[node.component];
  if (!factory) return null;
  return <React.Fragment key={key}>{factory(props, children)}</React.Fragment>;
}

export default function A2UISurface({ surface }) {
  const { root, data } = surface;
  const totalVotes = (data?.votesA ?? 0) + (data?.votesB ?? 0);
  return <div className="a2ui-surface">{renderNode(root, data ?? {}, { totalVotes }, "root")}</div>;
}

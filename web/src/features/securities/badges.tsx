import { humanize } from "./format";

const RATING_TAG: Record<string, string> = {
  strong_buy: "tag tag-accent",
  buy: "tag tag-accent",
  hold: "tag tag-neutral",
  sell: "tag tag-outline",
  strong_sell: "tag tag-outline",
  not_rated: "tag tag-neutral",
  under_review: "tag tag-neutral",
  restricted: "tag tag-neutral",
};

/** Canonical-rating badge (strong_buy/buy/hold/sell/strong_sell/...). */
export function RatingBadge({ rating }: { rating: string | null | undefined }) {
  const cls = (rating && RATING_TAG[rating]) || "tag tag-neutral";
  return (
    <span className={cls} data-testid="rating-badge" title={rating ?? undefined}>
      {humanize(rating)}
    </span>
  );
}

/** Rating action badge (initiation/upgrade/downgrade/...). */
export function ActionBadge({ action }: { action: string | null | undefined }) {
  return (
    <span className="tag tag-neutral" data-testid="action-badge" title={action ?? undefined}>
      {humanize(action)}
    </span>
  );
}

/** Correction marker for rating events / audit rows. */
export function CorrectionBadge() {
  return (
    <span className="tag tag-accent-2" data-testid="correction-badge">
      correction
    </span>
  );
}

/** Prominent delisted marker — delisted securities are never hidden. */
export function DelistedBadge() {
  return (
    <span className="tag tag-outline" data-testid="delisted-badge">
      Delisted
    </span>
  );
}

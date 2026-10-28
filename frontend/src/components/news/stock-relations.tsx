import { useState } from 'react';

import type { NewsEntry } from '@/lib/types/news';


type StockRelationsProps = {
  entry: NewsEntry;
  compact?: boolean;
  selectedStock?: string;
  showReasons?: boolean;
};


function safeHttpUrl(url: string) {
  return /^https?:\/\//.test(url) ? url : null;
}


export function StockRelations({
  entry, compact = false, selectedStock = '', showReasons = false,
}: StockRelationsProps) {
  const [expanded, setExpanded] = useState(false);
  const codes = [...new Set(entry.stock_codes?.length
    ? entry.stock_codes
    : [entry.stock_code])].sort();
  const ordered = selectedStock && codes.includes(selectedStock)
    ? [selectedStock, ...codes.filter(code => code !== selectedStock)]
    : codes;
  const hidden = compact && !expanded ? Math.max(0, ordered.length - 3) : 0;
  const visible = compact && !expanded ? ordered.slice(0, 3) : ordered;
  const relations = new Map(
    (entry.stock_relations ?? []).map(relation => [relation.stock_code, relation.reasons]),
  );

  return <span className={compact ? 'inline-flex flex-wrap items-center gap-1' : 'block'}>
    <span className="inline-flex flex-wrap items-center gap-1">
      <span>关联关注股：</span>
      {visible.map(code => <span key={code} className="rounded-sm border px-1 py-0.5 font-medium text-foreground">
        [{code}]
      </span>)}
      {hidden > 0 && <button
        type="button"
        className="underline underline-offset-2"
        aria-expanded={expanded}
        aria-label={`展开其余 ${hidden} 家关联关注股`}
        onClick={() => setExpanded(true)}
      >+{hidden}</button>}
      {compact && expanded && ordered.length > 3 && <button
        type="button"
        className="underline underline-offset-2"
        aria-expanded={expanded}
        aria-label="收起关联关注股"
        onClick={() => setExpanded(false)}
      >收起</button>}
    </span>
    {showReasons && ordered.map(code => {
      const reasons = relations.get(code) ?? [];
      if (reasons.length === 0) return null;
      return <span key={code} className="mt-1 block">
        <span className="font-medium text-foreground">{code}</span>
        {reasons.map(reason => {
          const key = `${reason.source_id}:${reason.source_record_id}:${reason.text}`;
          const url = safeHttpUrl(reason.url);
          return <span key={key} className="block">
            {url
              ? <a href={url} target="_blank" rel="noopener noreferrer" className="text-primary hover:underline">{reason.text}</a>
              : <span>{reason.text}</span>}
          </span>;
        })}
      </span>;
    })}
  </span>;
}

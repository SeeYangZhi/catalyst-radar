"use client";

import {
  Pagination,
  PaginationContent,
  PaginationEllipsis,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { PAGE_SIZE_OPTIONS } from "@/hooks/use-pagination";
import { cn } from "@/lib/utils";

export interface TablePaginationProps {
  className?: string;
  onPageChange: (p: number) => void;
  onPageSizeChange?: (n: number) => void;
  page: number;
  pageCount: number;
  pageSize: number;
  total: number;
}

// First, last, current ± 1, with ellipses filling the gaps. Caps the visible
// chips at ~7 even for very long tables.
function pageNumbers(page: number, pageCount: number): (number | "…")[] {
  if (pageCount <= 7) {
    return Array.from({ length: pageCount }, (_, i) => i + 1);
  }
  const out: (number | "…")[] = [1];
  const left = Math.max(2, page - 1);
  const right = Math.min(pageCount - 1, page + 1);
  if (left > 2) {
    out.push("…");
  }
  for (let p = left; p <= right; p += 1) {
    out.push(p);
  }
  if (right < pageCount - 1) {
    out.push("…");
  }
  out.push(pageCount);
  return out;
}

export function TablePagination({
  page,
  pageCount,
  pageSize,
  total,
  onPageChange,
  onPageSizeChange,
  className,
}: TablePaginationProps) {
  if (total === 0) {
    return null;
  }
  const first = (page - 1) * pageSize + 1;
  const last = Math.min(page * pageSize, total);

  return (
    <div
      className={cn(
        "flex flex-wrap items-center justify-between gap-2 px-1 py-2 text-ink-muted text-xs",
        className
      )}
    >
      <span>
        {first.toLocaleString()}–{last.toLocaleString()} of{" "}
        {total.toLocaleString()}
      </span>
      <div className="flex items-center gap-3">
        {onPageSizeChange && (
          <label className="flex items-center gap-1">
            Rows
            <Select
              onValueChange={(v) => onPageSizeChange(Number(v))}
              value={String(pageSize)}
            >
              <SelectTrigger className="h-7 w-[72px] text-xs" size="sm">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PAGE_SIZE_OPTIONS.map((n) => (
                  <SelectItem key={n} value={String(n)}>
                    {n}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
        )}
        <Pagination className="m-0 w-auto">
          <PaginationContent>
            <PaginationItem>
              <PaginationPrevious
                aria-disabled={page <= 1}
                className={
                  page <= 1 ? "pointer-events-none opacity-40" : undefined
                }
                href="#"
                onClick={(e) => {
                  e.preventDefault();
                  if (page > 1) {
                    onPageChange(page - 1);
                  }
                }}
              />
            </PaginationItem>
            {pageNumbers(page, pageCount).map((p, i) =>
              p === "…" ? (
                <PaginationItem key={`e${i}`}>
                  <PaginationEllipsis />
                </PaginationItem>
              ) : (
                <PaginationItem key={p}>
                  <PaginationLink
                    href="#"
                    isActive={p === page}
                    onClick={(e) => {
                      e.preventDefault();
                      onPageChange(p);
                    }}
                  >
                    {p}
                  </PaginationLink>
                </PaginationItem>
              )
            )}
            <PaginationItem>
              <PaginationNext
                aria-disabled={page >= pageCount}
                className={
                  page >= pageCount
                    ? "pointer-events-none opacity-40"
                    : undefined
                }
                href="#"
                onClick={(e) => {
                  e.preventDefault();
                  if (page < pageCount) {
                    onPageChange(page + 1);
                  }
                }}
              />
            </PaginationItem>
          </PaginationContent>
        </Pagination>
      </div>
    </div>
  );
}

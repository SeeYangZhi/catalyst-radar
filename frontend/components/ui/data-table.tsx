"use client";

import {
  type ColumnDef,
  flexRender,
  getCoreRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  type PaginationState,
  type SortingState,
  useReactTable,
  type VisibilityState,
} from "@tanstack/react-table";
import { ChevronDown } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TablePagination } from "@/components/ui/table-pagination";
import { DEFAULT_PAGE_SIZE } from "@/hooks/use-pagination";
import { cn } from "@/lib/utils";

export interface DataTableProps<TData> {
  // Wrap the table area in a className (mostly for max-width / spacing).
  className?: string;
  columns: ColumnDef<TData, unknown>[];
  data: TData[];
  // Replaces the default "No results" message.
  emptyMessage?: React.ReactNode;
  // Shows a "Columns" dropdown to toggle visibility. Default true when any
  // column has `enableHiding` (left as default true by TanStack).
  enableColumnVisibility?: boolean;
  // Initial sort.
  initialSorting?: SortingState;
  // Minimum width before horizontal scrolling kicks in.
  minWidth?: number | string;
  // Initial page size (defaults to the shared DEFAULT_PAGE_SIZE).
  pageSize?: number;
  // Render a sub-row directly under each row when this returns a node.
  // Used by the Companies page for the per-company sources panel.
  renderSubRow?: (row: TData) => React.ReactNode;
  // Optional content rendered above the table (filter pills, date pickers).
  toolbar?: React.ReactNode;
  // Server-side pagination mode. When provided, the parent owns the
  // pagination state and refetches on change; the table just renders the
  // current page slice. Omit for client-side (default).
  serverPagination?: {
    pageIndex: number; // zero-based
    pageSize: number;
    pageCount: number; // total pages (or -1 when unknown)
    total: number;
    onChange: (next: { pageIndex: number; pageSize: number }) => void;
  };
}

export function DataTable<TData>({
  columns,
  data,
  toolbar,
  emptyMessage = "No results.",
  enableColumnVisibility = true,
  initialSorting = [],
  minWidth,
  className,
  pageSize = DEFAULT_PAGE_SIZE,
  renderSubRow,
  serverPagination,
}: DataTableProps<TData>) {
  const [sorting, setSorting] = React.useState<SortingState>(initialSorting);
  const [columnVisibility, setColumnVisibility] =
    React.useState<VisibilityState>({});

  const manualPagination = serverPagination !== undefined;
  const paginationState: PaginationState | undefined = manualPagination
    ? {
        pageIndex: serverPagination.pageIndex,
        pageSize: serverPagination.pageSize,
      }
    : undefined;

  // TanStack Table's `useReactTable` returns fresh handles each render by
  // design (sorted/filtered models depend on live state). That trips the
  // "incompatible-library" rule, but the library is intentionally non-memoized
  // and any wrapping with useMemo would break its own internal caching.
  // eslint-disable-next-line react-hooks/incompatible-library
  const table = useReactTable({
    data,
    columns,
    onSortingChange: setSorting,
    onColumnVisibilityChange: setColumnVisibility,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    // In server-side mode the parent supplies the current page slice; the
    // built-in pagination model would re-slice the array.
    getPaginationRowModel: manualPagination ? undefined : getPaginationRowModel(),
    manualPagination,
    pageCount: manualPagination ? serverPagination.pageCount : undefined,
    onPaginationChange: manualPagination
      ? (updater) => {
          const next =
            typeof updater === "function"
              ? updater({
                  pageIndex: serverPagination.pageIndex,
                  pageSize: serverPagination.pageSize,
                })
              : updater;
          serverPagination.onChange(next);
        }
      : undefined,
    initialState: manualPagination ? undefined : { pagination: { pageSize } },
    state: {
      sorting,
      columnVisibility,
      ...(paginationState ? { pagination: paginationState } : {}),
    },
  });

  const hideableColumns = table.getAllColumns().filter((c) => c.getCanHide());

  const minWidthStyle =
    typeof minWidth === "number"
      ? { minWidth: `${minWidth}px` }
      : typeof minWidth === "string"
        ? { minWidth }
        : undefined;

  return (
    <div className={cn("space-y-2", className)}>
      {(toolbar || (enableColumnVisibility && hideableColumns.length > 0)) && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">{toolbar}</div>
          {enableColumnVisibility && hideableColumns.length > 0 && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button size="sm" variant="secondary">
                  Columns
                  <ChevronDown className="h-3.5 w-3.5" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {hideableColumns.map((column) => (
                  <DropdownMenuCheckboxItem
                    checked={column.getIsVisible()}
                    className="capitalize"
                    key={column.id}
                    onCheckedChange={(value) =>
                      column.toggleVisibility(!!value)
                    }
                  >
                    {column.id.replace(/_/g, " ")}
                  </DropdownMenuCheckboxItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      )}

      <div className="overflow-x-auto rounded-[15px] border border-border">
        <Table style={minWidthStyle}>
          <TableHeader className="bg-surface-1">
            {table.getHeaderGroups().map((headerGroup) => (
              <TableRow key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <TableHead key={header.id}>
                    {header.isPlaceholder
                      ? null
                      : flexRender(
                          header.column.columnDef.header,
                          header.getContext()
                        )}
                  </TableHead>
                ))}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {table.getRowModel().rows.length === 0 ? (
              <TableRow>
                <TableCell
                  className="h-24 text-center text-ink-muted"
                  colSpan={columns.length}
                >
                  {emptyMessage}
                </TableCell>
              </TableRow>
            ) : (
              table.getRowModel().rows.map((row) => (
                <React.Fragment key={row.id}>
                  <TableRow data-state={row.getIsSelected() && "selected"}>
                    {row.getVisibleCells().map((cell) => (
                      // align-top lets multi-line cells (e.g. a company name
                      // with a wrapped description below it) align cleanly
                      // against single-line sibling cells.
                      <TableCell className="align-top" key={cell.id}>
                        {flexRender(
                          cell.column.columnDef.cell,
                          cell.getContext()
                        )}
                      </TableCell>
                    ))}
                  </TableRow>
                  {renderSubRow && (
                    <SubRow
                      colSpan={row.getVisibleCells().length}
                      render={renderSubRow}
                      row={row.original}
                    />
                  )}
                </React.Fragment>
              ))
            )}
          </TableBody>
        </Table>
      </div>

      <TablePagination
        onPageChange={(p) => table.setPageIndex(p - 1)}
        onPageSizeChange={(n) => table.setPageSize(n)}
        page={table.getState().pagination.pageIndex + 1}
        pageCount={table.getPageCount() || 1}
        pageSize={table.getState().pagination.pageSize}
        total={
          manualPagination
            ? serverPagination.total
            : table.getFilteredRowModel().rows.length
        }
      />
    </div>
  );
}

function SubRow<TData>({
  row,
  colSpan,
  render,
}: {
  row: TData;
  colSpan: number;
  render: (row: TData) => React.ReactNode;
}) {
  const content = render(row);
  if (!content) {
    return null;
  }
  return (
    <TableRow>
      <TableCell className="p-0" colSpan={colSpan}>
        {content}
      </TableCell>
    </TableRow>
  );
}

// Re-export ColumnDef so callers don't need a second import.
export type { ColumnDef } from "@tanstack/react-table";

// Tiny header helper for sortable columns — pass to ColumnDef.header.
export function sortableHeader<TData>(label: React.ReactNode) {
  function SortableHeader({
    column,
  }: {
    column: import("@tanstack/react-table").Column<TData, unknown>;
  }) {
    const dir = column.getIsSorted();
    return (
      <Button
        className="-ml-2 h-7 px-2"
        onClick={() => column.toggleSorting(dir === "asc")}
        size="sm"
        variant="ghost"
      >
        {label}
        <ChevronDown
          className={cn(
            "h-3.5 w-3.5 transition-transform",
            dir === "asc" && "rotate-180",
            !dir && "opacity-40"
          )}
        />
      </Button>
    );
  }
  return SortableHeader;
}

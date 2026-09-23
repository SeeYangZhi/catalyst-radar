/**
 * components/ui/table-pagination.tsx — pure presentational pager used by
 * DataTable (review/companies/events pages). Clear props, no router needed.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { TablePagination } from "@/components/ui/table-pagination";

function renderPager(over: Partial<Parameters<typeof TablePagination>[0]> = {}) {
  const onPageChange = vi.fn();
  const props = {
    onPageChange,
    page: 1,
    pageCount: 4,
    pageSize: 25,
    total: 100,
    ...over,
  };
  const view = render(<TablePagination {...props} />);
  return { onPageChange, view };
}

describe("TablePagination", () => {
  test("renders nothing when the table is empty", () => {
    const { view } = renderPager({ total: 0, pageCount: 0 });
    expect(view.container).toBeEmptyDOMElement();
  });

  test("shows the visible row range and total", () => {
    renderPager({ page: 2 });
    expect(screen.getByText(/26–50 of 100/)).toBeInTheDocument();
  });

  test("clicking next advances one page", () => {
    const { onPageChange } = renderPager({ page: 2 });
    fireEvent.click(screen.getByLabelText("Go to next page"));
    expect(onPageChange).toHaveBeenCalledWith(3);
  });

  test("previous is inert on the first page", () => {
    const { onPageChange } = renderPager({ page: 1 });
    const prev = screen.getByLabelText("Go to previous page");
    expect(prev).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(prev);
    expect(onPageChange).not.toHaveBeenCalled();
  });

  test("next is inert on the last page", () => {
    const { onPageChange } = renderPager({ page: 4 });
    const next = screen.getByLabelText("Go to next page");
    expect(next).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(next);
    expect(onPageChange).not.toHaveBeenCalled();
  });

  test("clicking a page number jumps straight to it", () => {
    const { onPageChange } = renderPager({ page: 1 });
    fireEvent.click(screen.getByText("3"));
    expect(onPageChange).toHaveBeenCalledWith(3);
  });

  test("long page lists collapse middle pages into ellipses", () => {
    renderPager({ page: 10, pageCount: 20, total: 500 });
    // first, current ± 1, last — everything else elided.
    for (const visible of ["1", "9", "10", "11", "20"]) {
      expect(screen.getByText(visible)).toBeInTheDocument();
    }
    expect(screen.queryByText("5")).not.toBeInTheDocument();
    expect(screen.getAllByText("More pages").length).toBe(2);
  });
});

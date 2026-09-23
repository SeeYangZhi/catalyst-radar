/**
 * components/ui/badge.tsx — presentational status pill.
 * Variants are the repo-wide status palette (frontend/AGENTS.md).
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { Badge } from "@/components/ui/badge";

describe("Badge", () => {
  test("renders its children", () => {
    render(<Badge>success</Badge>);
    expect(screen.getByText("success")).toBeInTheDocument();
  });

  test("defaults to the neutral variant", () => {
    render(<Badge>plain</Badge>);
    expect(screen.getByText("plain")).toHaveClass("bg-surface-2");
  });

  test("applies the danger variant classes", () => {
    render(<Badge variant="danger">failed</Badge>);
    const el = screen.getByText("failed");
    expect(el).toHaveClass("text-destructive");
    expect(el).not.toHaveClass("bg-surface-2");
  });

  test("merges a custom className with variant classes", () => {
    render(
      <Badge className="ml-2" variant="accent">
        catalyst
      </Badge>
    );
    const el = screen.getByText("catalyst");
    expect(el).toHaveClass("ml-2");
    expect(el).toHaveClass("text-accent");
  });
});

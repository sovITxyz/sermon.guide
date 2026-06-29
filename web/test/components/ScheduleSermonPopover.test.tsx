import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ScheduleSermonPopover } from "../../components/ScheduleSermonPopover";

/**
 * ScheduleSermonPopover (Phase 47) tests. The popover is presentation-only — it
 * collects date/title/series and resolves the caller's `onSubmit` (the caller
 * owns the fetch + the sermon's document_id), mirroring QuickCreatePopover. So
 * these assert the contract without any network:
 *  - the date defaults to the passed date, the title to the sermon's title;
 *  - submit calls `onSubmit` with event_date/title and series (null when blank);
 *  - a returned error string renders inline and re-enables the button;
 *  - Cancel calls `onClose`.
 */

describe("ScheduleSermonPopover", () => {
  function renderPopover(
    overrides: {
      defaultDate?: string;
      defaultTitle?: string;
      onSubmit?: (input: {
        event_date: string;
        title: string;
        series: string | null;
      }) => Promise<string | null>;
      onClose?: () => void;
    } = {},
  ) {
    const onSubmit = overrides.onSubmit ?? vi.fn(async () => null);
    const onClose = overrides.onClose ?? vi.fn();
    render(
      <ScheduleSermonPopover
        defaultDate={overrides.defaultDate ?? "2026-07-05"}
        defaultTitle={overrides.defaultTitle ?? "Grace in Romans"}
        onSubmit={onSubmit}
        onClose={onClose}
      />,
    );
    return { onSubmit, onClose };
  }

  it("prefills the date and the sermon title", () => {
    renderPopover({ defaultDate: "2026-07-05", defaultTitle: "Grace in Romans" });
    expect(screen.getByLabelText("Date")).toHaveValue("2026-07-05");
    expect(screen.getByLabelText("Event title")).toHaveValue("Grace in Romans");
  });

  it("submits event_date, title, and series: null when series is blank", async () => {
    const { onSubmit } = renderPopover();
    fireEvent.click(screen.getByRole("button", { name: "Schedule" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit).toHaveBeenCalledWith({
      event_date: "2026-07-05",
      title: "Grace in Romans",
      series: null,
    });
  });

  it("forwards an edited date, title, and a non-blank series", async () => {
    const { onSubmit } = renderPopover();
    fireEvent.change(screen.getByLabelText("Date"), { target: { value: "2026-12-25" } });
    fireEvent.change(screen.getByLabelText("Event title"), { target: { value: "Advent" } });
    fireEvent.change(screen.getByLabelText(/series/i), { target: { value: "Christmas" } });
    fireEvent.click(screen.getByRole("button", { name: "Schedule" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit).toHaveBeenCalledWith({
      event_date: "2026-12-25",
      title: "Advent",
      series: "Christmas",
    });
  });

  it("renders a returned error inline and re-enables the button", async () => {
    const onSubmit = vi.fn(async () => "Could not schedule the sermon.");
    renderPopover({ onSubmit });
    fireEvent.click(screen.getByRole("button", { name: "Schedule" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not schedule the sermon.");
    // The submit button is back to its idle label (not stuck on "Scheduling…").
    expect(screen.getByRole("button", { name: "Schedule" })).toBeEnabled();
  });

  it("calls onClose when Cancel is clicked", () => {
    const { onClose } = renderPopover();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

import { useCallback, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function useUnpinConfirmation() {
  const [pending, setPending] = useState<{ label: string; confirm: () => void } | null>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const requestUnpin = useCallback((label: string, confirm: () => void) => {
    setPending({ label, confirm });
  }, []);

  return {
    requestUnpin,
    dialog: (
      <Dialog open={pending !== null} onOpenChange={(open) => !open && setPending(null)}>
        <DialogContent
          onClick={(event) => event.stopPropagation()}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            cancelRef.current?.focus();
          }}
        >
          <DialogHeader>
            <DialogTitle>Unpin session?</DialogTitle>
            <DialogDescription>
              Remove <span className="font-medium wrap-anywhere">{pending?.label}</span> from
              Pinned?
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="border-t-0 bg-transparent">
            <Button ref={cancelRef} type="button" variant="ghost" onClick={() => setPending(null)}>
              No
            </Button>
            <Button
              type="button"
              onClick={() => {
                setPending(null);
                pending?.confirm();
              }}
            >
              Yes
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    ),
  };
}

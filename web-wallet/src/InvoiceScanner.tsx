import { useEffect, useRef, useState } from "react";
import { Camera, Upload } from "lucide-react";
import QrScanner from "qr-scanner";
import { psInvoiceDetails } from "@cashu/cashu-ts/tmp-cash";

function invoiceFromQR(value: string): string {
  const invoice = value
    .trim()
    .replace(/^lightning:/i, "")
    .toLowerCase();
  try {
    psInvoiceDetails(invoice);
    return invoice;
  } catch {
    throw new Error(
      "This QR code does not contain a valid BOLT11 Lightning invoice.",
    );
  }
}

export function InvoiceScanner({
  onScan,
  disabled,
}: {
  onScan: (invoice: string) => void;
  disabled: boolean;
}) {
  const video = useRef<HTMLVideoElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const imageRequest = useRef(0);
  const [scanning, setScanning] = useState(false);
  const [reading, setReading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    // Ignore an image result after the dialog closes or a review starts.
    imageRequest.current += 1;
    setReading(false);
    return () => {
      imageRequest.current += 1;
    };
  }, [disabled]);

  useEffect(() => {
    if (!scanning || disabled) return;
    let active = true;
    const scanner = new QrScanner(
      video.current!,
      ({ data }) => {
        if (!active) return;
        let invoice: string;
        try {
          invoice = invoiceFromQR(data);
        } catch (err) {
          setError((err as Error).message);
          return;
        }
        active = false;
        void scanner.pause(true);
        setScanning(false);
        setError("");
        onScan(invoice);
      },
      {
        preferredCamera: "environment",
        maxScansPerSecond: 10,
        highlightScanRegion: true,
        onDecodeError: () => {
          // Frames without a readable QR code are normal while aiming.
        },
      },
    );
    scanner.setInversionMode("both");
    scanner.start().catch(() => {
      if (!active) return;
      setScanning(false);
      setError(
        "Could not access the camera. Allow camera access on HTTPS or localhost, upload a QR image, or paste the invoice.",
      );
    });
    return () => {
      active = false;
      void scanner.pause(true);
      scanner.destroy();
    };
  }, [scanning, disabled, onScan]);

  async function scanImage(file: File) {
    const request = ++imageRequest.current;
    setScanning(false);
    setReading(true);
    setError("");
    try {
      let data: string;
      try {
        ({ data } = await QrScanner.scanImage(file, {
          returnDetailedScanResult: true,
        }));
      } catch {
        throw new Error(
          "Could not read a QR code. Choose a clear image of a Lightning invoice.",
        );
      }
      if (request !== imageRequest.current) return;
      onScan(invoiceFromQR(data));
    } catch (err) {
      if (request === imageRequest.current) setError((err as Error).message);
    } finally {
      if (request === imageRequest.current) setReading(false);
    }
  }

  return (
    <div className="invoice-scanner">
      <div className="invoice-scan-actions">
        <button
          type="button"
          className="button secondary"
          disabled={disabled || reading}
          onClick={() => {
            setError("");
            setScanning((current) => !current);
          }}
        >
          <Camera size={16} />
          {scanning ? "Stop scanning" : "Scan QR code"}
        </button>
        <button
          type="button"
          className="button secondary"
          disabled={disabled || reading}
          onClick={() => fileInput.current?.click()}
        >
          <Upload size={16} />
          {reading ? "Reading QR…" : "Upload QR image"}
        </button>
        <input
          ref={fileInput}
          type="file"
          hidden
          accept="image/*"
          aria-label="Invoice QR image"
          disabled={disabled || reading}
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) void scanImage(file);
          }}
        />
      </div>
      {scanning && (
        <>
          <div className="invoice-camera">
            <video
              ref={video}
              muted
              playsInline
              aria-label="Invoice QR camera"
            />
          </div>
          <p className="small muted" role="status">
            Point your camera at a Lightning invoice QR code.
          </p>
        </>
      )}
      {error && (
        <p className="small" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

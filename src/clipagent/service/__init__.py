"""Zero-touch service around /make-clips: watch an inbox, make clips, gate them, schedule them, deliver them through OFFICIAL TikTok APIs only,
learn from official metrics, and keep the disk safe. Default delivery mode is PRIVATE_OR_DRAFT_QUEUE; public Direct Post is only enabled when an
audited client, a consent record and a compliant UX flow are recorded in the configuration (never assumed, never faked)."""

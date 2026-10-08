import Interview from "../Interview";

/** Onboarding is round one of the profile interview; it continues later under "Strengthen". */
export default function FollowupsStep({ onDone }: { onDone: () => void; onBack: () => void }) {
  return <Interview onFinish={onDone} />;
}

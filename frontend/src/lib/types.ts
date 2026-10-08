export interface User {
  id: number;
  email: string;
  timezone: string;
  digest_hour: number;
  digest_enabled: boolean;
  is_admin: boolean;
}

export interface Status {
  is_new_grad: boolean | null;
  graduation_date: string | null;
  need_job_by: string | null;
  target_roles: string[];
  seniority: string[];
  target_locations: string[];
  remote_preference: string | null;
  willing_to_relocate: boolean | null;
  needs_visa_sponsorship: boolean | null;
  work_authorization: string | null;
  salary_min: number | null;
  salary_target: number | null;
  prestige_preference: number | null;
  company_sizes: string[];
  industries: string[];
  notes: string | null;
}

export const emptyStatus: Status = {
  is_new_grad: null,
  graduation_date: null,
  need_job_by: null,
  target_roles: [],
  seniority: [],
  target_locations: [],
  remote_preference: null,
  willing_to_relocate: null,
  needs_visa_sponsorship: null,
  work_authorization: null,
  salary_min: null,
  salary_target: null,
  prestige_preference: null,
  company_sizes: [],
  industries: [],
  notes: null,
};

export interface Education { school: string; degree: string; field: string; start: string; end: string; gpa: string }
export interface Experience {
  kind: string; company: string; title: string; start: string; end: string;
  location: string; summary: string; bullets: string[]; technologies: string[];
}
export interface Project { name: string; description: string; technologies: string[]; url: string }
export interface Skill { name: string; level: string }
export interface Background {
  name: string;
  summary: string;
  education: Education[];
  experience: Experience[];
  projects: Project[];
  skills: Skill[];
  certifications: string[];
}

export const emptyBackground: Background = {
  name: "", summary: "", education: [], experience: [], projects: [], skills: [], certifications: [],
};

export interface Profile { status: Partial<Status>; background: Partial<Background>; version: number }
export interface DocumentOut {
  id: string;
  kind: string;
  filename: string;
  size: number;
  chars: number;
  tags: Record<string, string>;
  created_at: string;
  duplicate: boolean;
}
export interface LinkOut { id: string; kind: string; url: string; fetched: boolean; fetch_error: string | null }
export interface TaskOut<R = unknown> { id: number; kind: string; status: "queued" | "running" | "done" | "failed"; result: R | null; error: string | null }
export interface FollowupQuestion { question: string; why: string }

export interface AiStatus {
  configured: boolean;
  source: "file" | "env" | "none";
  last_call: { ok: boolean; error_kind: string | null; at: string } | null;
}
export interface AiCheckResult { ok: boolean; error_kind?: string; error?: string }

export interface MatchJob {
  id: number;
  title: string;
  company: string;
  location: string;
  workplace_type: string | null;
  seniority: string | null;
  salary_min: number | null;
  salary_max: number | null;
  salary_currency: string | null;
  url: string;
  posted_at: string | null;
  is_active: boolean;
  sponsorship?: "sponsors" | "refuses" | "likely" | "unlikely" | null;
}
export type MatchStatus = "new" | "saved" | "applied" | "dismissed";
export interface Match {
  id: number;
  status: MatchStatus;
  score: number;
  confidence: number;
  verdict: "strong" | "good" | "stretch" | "no";
  reasons: string[];
  gaps: string[];
  hire_verdict?: "yes" | "maybe" | "no" | "";
  recruiter_take?: string;
  stale: boolean;
  has_cover_letter: boolean;
  scored_at: string;
  job: MatchJob;
}
export type RequirementStatus = "met" | "partial" | "unknown" | "unmet";
export interface Requirement {
  requirement: string;
  importance: "must" | "nice";
  status: RequirementStatus;
  evidence: string;
  question: string;
}
export interface MatchDetail extends Match {
  unknowns: { question: string; why: string }[];
  requirements: Requirement[];
  description: string;
}
export interface MatchList {
  items: Match[];
  total: number;
  counts: Record<MatchStatus, number>;
  summary: { last_run_at?: string; scored?: number; pending?: number; failed?: number; career_stage?: string | null };
}

export type Tone = "professional" | "warm" | "concise" | "enthusiastic";
export interface CoverLetter {
  content: string;
  tone: Tone;
  edited: boolean;
  words: number;
  stale: boolean;
  generated_at: string;
  updated_at: string;
}
export interface CoverLetterState { letter: CoverLetter | null; pending_task_id: number | null }

export interface QuestionItem {
  id: number;
  question: string;
  why: string;
  jobs: { match_id: number; title: string; company: string }[];
}
export interface AnswerResult { answered: number; skipped: number; remaining: number }
export interface Settings {
  digest_enabled: boolean;
  digest_hour: number;
  timezone: string;
  question_emails_enabled: boolean;
  match_budget_enabled: boolean;
  match_budget: number;
}
export interface Fact { id: number; question: string; answer: string; source: string }

export interface DossierItem { label: string; detail: string; confidence: number }
export interface Dossier {
  headline?: string;
  years_experience?: string;
  skills?: DossierItem[];
  experience?: DossierItem[];
  logistics?: DossierItem[];
  strengths?: string[];
  concerns?: string[];
  hire_view?: string;
}
export interface InterviewQuestion { question: string; why: string; dimension: string }
export interface Interview {
  readiness: number;
  dimensions: Record<string, number>;
  dossier: Dossier;
  questions: InterviewQuestion[];
  audited: boolean;
  stale: boolean;
}

export interface ScheduleTask { kind: string; status: string; run_after: string }
export interface ScheduleRow {
  user_id: number;
  email: string;
  last_seen_at: string | null;
  last_run_at: string | null;
  last_scored: number | null;
  pending: number | null;
  budget_left: number | null;
  last_planned_at: string | null;
  next_due_at: string | null;
  reason: string;
  skip: string;
  active: ScheduleTask[];
}
export interface Schedule {
  backoff_until: string | null;
  in_flight: number;
  max_per_tick: number;
  next_tick: string | null;
  users: ScheduleRow[];
}

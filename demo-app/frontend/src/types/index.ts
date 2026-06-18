/** Shared TypeScript types matching backend Pydantic schemas. */

export interface User {
  id: string;
  email: string;
  display_name: string;
  created_at: string;
  is_active: boolean;
}

export interface Project {
  id: string;
  name: string;
  description: string | null;
  owner_id: string;
  created_at: string;
}

export interface Task {
  id: string;
  project_id: string;
  title: string;
  description: string | null;
  status: "todo" | "doing" | "done";
  priority: number;
  assignee_id: string | null;
  due_date: string | null;
  created_at: string;
  updated_at: string;
  /** Populated on detail endpoint */
  comments?: Comment[];
  tags?: Tag[];
}

export interface Comment {
  id: string;
  task_id: string;
  author_id: string;
  content: string;
  created_at: string;
}

export interface Tag {
  id: number;
  name: string;
  color: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: "bearer";
}

export type TaskStatus = "todo" | "doing" | "done";

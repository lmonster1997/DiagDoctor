import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { User } from "@/types";

interface AuthState {
  token: string | null;
  currentUser: User | null;
  setAuth: (token: string, user: User) => void;
  logout: () => void;
  isAuthenticated: () => boolean;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      token: null,
      currentUser: null,

      setAuth: (token: string, user: User) => {
        set({ token, currentUser: user });
      },

      logout: () => {
        set({ token: null, currentUser: null });
      },

      isAuthenticated: () => {
        return get().token !== null;
      },
    }),
    {
      name: "taskflow-auth",
      // Only persist token and currentUser
      partialize: (state) => ({
        token: state.token,
        currentUser: state.currentUser,
      }),
    }
  )
);

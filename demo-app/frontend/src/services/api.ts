import axios from "axios";
import { useAuthStore } from "@/stores/authStore";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || "http://localhost:8000",
  headers: {
    "Content-Type": "application/json",
  },
  timeout: 15000,
});

// Request interceptor: auto-inject Bearer token
api.interceptors.request.use(
  (config) => {
    const token = useAuthStore.getState().token;
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => {
    console.error("[API_REQUEST_FAIL]", { error: error.message });
    return Promise.reject(error);
  }
);

// Response interceptor: handle 401 → redirect to login
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      console.warn("[API_UNAUTHORIZED]", { url: error.config?.url });
      useAuthStore.getState().logout();
      // Redirect to login if not already there
      if (window.location.pathname !== "/login") {
        window.location.href = "/login";
      }
    }
    console.error("[API_RESPONSE_ERROR]", {
      status: error.response?.status,
      url: error.config?.url,
      error: error.message,
    });
    return Promise.reject(error);
  }
);

export default api;

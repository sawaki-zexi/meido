import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App.tsx";
import { LandingPage } from "./landing/LandingPage.tsx";
import "./styles.css";
import "./landing/landing.css";

const isAppRoute = window.location.pathname === "/app" || window.location.pathname.startsWith("/app/");
document.title = isAppRoute ? "Meido 角色" : "Meido";

createRoot(document.getElementById("root")!).render(
  <StrictMode>{isAppRoute ? <App /> : <LandingPage />}</StrictMode>,
);

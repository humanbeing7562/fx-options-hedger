import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.jsx";
import HoverTip from "./components/HoverTip.jsx";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
    <HoverTip />
  </React.StrictMode>
);
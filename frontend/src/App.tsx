import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "@/components/AppShell";
import { RequireAuth } from "@/components/RequireAuth";
import { LoginPage } from "@/auth/LoginPage";
import { useAuth } from "@/auth/AuthContext";
import { CustomerCreatePage } from "@/customers/CustomerCreatePage";
import { CustomerDetailPage } from "@/customers/CustomerDetailPage";
import { CustomerListPage } from "@/customers/CustomerListPage";
import { DailyRegisterPage } from "@/daily/DailyRegisterPage";
import { IssuesPage } from "@/sync/IssuesPage";

/**
 * Four screens and a login.
 *
 * `/today` is the landing route because the daily round is the reason this app
 * is opened. `/attention` is P5's addition: the durable home of operations the
 * server refused, which must outlive the sync that produced them. Payments,
 * statements, corrections, reminders, dashboards and the platform surface are
 * not routed here: their packages have not run.
 */
export function App() {
  const { session } = useAuth();

  return (
    <Routes>
      <Route
        path="/login"
        element={session ? <Navigate to="/today" replace /> : <LoginPage />}
      />
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route path="/today" element={<DailyRegisterPage />} />
        <Route path="/customers" element={<CustomerListPage />} />
        <Route path="/customers/new" element={<CustomerCreatePage />} />
        <Route path="/customers/:customerId" element={<CustomerDetailPage />} />
        <Route path="/attention" element={<IssuesPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/today" replace />} />
    </Routes>
  );
}

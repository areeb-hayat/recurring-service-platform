import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "@/components/AppShell";
import { RequireAuth } from "@/components/RequireAuth";
import { LoginPage } from "@/auth/LoginPage";
import { useAuth } from "@/auth/AuthContext";
import { CustomerCreatePage } from "@/customers/CustomerCreatePage";
import { CustomerDetailPage } from "@/customers/CustomerDetailPage";
import { CustomerListPage } from "@/customers/CustomerListPage";
import { DailyRegisterPage } from "@/daily/DailyRegisterPage";
import { DashboardPage } from "@/dashboard/DashboardPage";
import { OperatingCostsPage } from "@/costs/OperatingCostsPage";
import { RecordPaymentPage } from "@/payments/RecordPaymentPage";
import { RemindersPage } from "@/reminders/RemindersPage";
import { StatementsPage } from "@/statements/StatementsPage";
import { IssuesPage } from "@/sync/IssuesPage";

/**
 * The screens, and a login.
 *
 * `/today` stays the landing route: the daily round is still the reason this app
 * is opened, and the owner's numbers are something you go and look at rather
 * than something you are shown while standing at a door. `/attention` is P5's
 * durable home for operations the server refused.
 *
 * P6 added the owner-facing half — the overview, issued statements, recording a
 * payment, and what the business pays its own providers. P7 adds `/reminders`:
 * where each customer stands in the month's schedule, and the one manual action
 * on it, a re-attempt of a failed delivery. Reminders themselves are sent by the
 * server's daily run, so there is no route here that sends one.
 *
 * Search, voice and the platform surface are still absent: their packages have
 * not run.
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
        <Route path="/overview" element={<DashboardPage />} />
        <Route path="/customers" element={<CustomerListPage />} />
        <Route path="/customers/new" element={<CustomerCreatePage />} />
        <Route path="/customers/:customerId" element={<CustomerDetailPage />} />
        <Route path="/customers/:customerId/pay" element={<RecordPaymentPage />} />
        <Route path="/statements" element={<StatementsPage />} />
        <Route path="/reminders" element={<RemindersPage />} />
        <Route path="/operating-costs" element={<OperatingCostsPage />} />
        <Route path="/attention" element={<IssuesPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/today" replace />} />
    </Routes>
  );
}

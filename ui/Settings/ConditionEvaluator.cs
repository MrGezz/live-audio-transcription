using System.Collections.Generic;
using System.Text.Json;

namespace LiveTranscription.Ui.Settings;

/// <summary>
/// Decides whether a field's <c>showIf</c> is currently satisfied.
/// </summary>
/// <remarks>
/// <para>
/// Most fields carry one, and their values are type-heterogeneous:
/// <c>{"vad": [true]}</c> is a boolean, <c>{"capture": ["file"]}</c> is a
/// string, and both arrive as <see cref="JsonElement"/>. That is the whole
/// difficulty. The obvious implementation compares
/// <c>current.ToString() == allowed.ToString()</c>, which looks right and is
/// wrong in exactly one direction: System.Text.Json renders a boolean as
/// "True"/"False" and a JSON literal as "true"/"false", so every
/// boolean-gated field evaluates to hidden and stays hidden forever. That is
/// 12 of the 26 - the entire VAD group, the overlay group and the save group -
/// disappearing with no error anywhere.
/// </para>
/// <para>
/// So the comparison is by <see cref="JsonValueKind"/> first and by typed
/// value second, and <see cref="Match"/> is covered by a startup self-test in
/// <see cref="SettingsVm"/> rather than left to be noticed.
/// </para>
/// <para>
/// Deliberately NOT transitive: a field whose condition is met shows even if
/// the field it depends on is itself hidden. That is what
/// <c>webui/app.js:433 visible()</c> does, and the two panels reading the same
/// schema differently would be worse than either rule on its own.
/// </para>
/// </remarks>
public static class ConditionEvaluator
{
    /// <param name="showIf">the field's condition map: key -> allowed values</param>
    /// <param name="current">the live value of every setting, by key</param>
    public static bool IsSatisfied(
        IReadOnlyDictionary<string, JsonElement[]> showIf,
        IReadOnlyDictionary<string, JsonElement> current)
    {
        if (showIf.Count == 0)
        {
            return true;
        }

        foreach (KeyValuePair<string, JsonElement[]> cond in showIf)
        {
            if (!current.TryGetValue(cond.Key, out JsonElement live))
            {
                // The field it depends on is not in the settings document at
                // all. Hiding is the safe answer: showing a control whose
                // gate cannot be evaluated offers an edit that may not apply.
                return false;
            }

            bool hit = false;
            foreach (JsonElement allowed in cond.Value)
            {
                if (Match(live, allowed))
                {
                    hit = true;
                    break;
                }
            }

            if (!hit)
            {
                return false;
            }
        }

        return true;
    }

    /// <summary>
    /// Equality between two JSON values, by kind. Public because the startup
    /// self-test asserts on it directly.
    /// </summary>
    public static bool Match(JsonElement a, JsonElement b)
    {
        // True and False are DISTINCT kinds in System.Text.Json - there is no
        // single Boolean kind - so this one check already settles both.
        if (a.ValueKind != b.ValueKind)
        {
            return false;
        }

        return a.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => true,
            JsonValueKind.Null => true,
            JsonValueKind.String => string.Equals(a.GetString(), b.GetString(),
                                                  System.StringComparison.Ordinal),
            // Compared as double so that a schema literal of 1 still matches a
            // live value of 1.0, which is how a number that has been through
            // Python's float() comes back.
            JsonValueKind.Number => a.TryGetDouble(out double x)
                                    && b.TryGetDouble(out double y)
                                    && x.Equals(y),
            _ => string.Equals(a.GetRawText(), b.GetRawText(),
                               System.StringComparison.Ordinal),
        };
    }
}

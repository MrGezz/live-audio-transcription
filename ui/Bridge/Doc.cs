using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text.Json;

namespace LiveTranscription.Ui.Bridge;

/// <summary>
/// A JSON document that arrived from Python, read defensively.
/// </summary>
/// <remarks>
/// <para>
/// Every open-ended payload crosses the bridge as a JSON string, because the
/// shape of a transcript entry or a status dict belongs to the engine and
/// freezing it into a CLR type would mean rebuilding this assembly every time
/// a field is added. The cost of that choice is paid here: nothing may assume
/// a key is present, or that it holds the type it held last week.
/// </para>
/// <para>
/// So every accessor takes a fallback and no accessor throws. That is not
/// defensive habit - these are read during a layout pass, on the dispatcher
/// thread, and an exception there takes the window down and the transcription
/// process with it. A meter frame that renders a zero is a bug worth an
/// afternoon; one that kills a two-hour recording is not the same kind of bug.
/// </para>
/// </remarks>
public readonly struct Doc
{
    private readonly JsonElement _el;
    private readonly bool _ok;

    private Doc(JsonElement el, bool ok)
    {
        _el = el;
        _ok = ok;
    }

    /// <summary>A document that holds nothing. Every read returns its fallback.</summary>
    public static Doc None => default;

    public bool Exists => _ok;

    /// <summary>The underlying element, for callers that need the raw kind.</summary>
    public JsonElement Element => _el;

    public JsonValueKind Kind => _ok ? _el.ValueKind : JsonValueKind.Undefined;

    /// <summary>
    /// Parse a document. Malformed JSON yields <see cref="None"/> rather than
    /// throwing - the caller is a UI push, and there is nobody to catch it.
    /// </summary>
    public static Doc Parse(string? json)
    {
        if (string.IsNullOrWhiteSpace(json))
        {
            return None;
        }

        try
        {
            // Cloned because the JsonDocument that owns the buffer is disposed
            // on the way out of this method; an un-cloned JsonElement would
            // read freed memory later, which fails as garbage rather than as
            // an exception.
            using var doc = JsonDocument.Parse(json);
            return new Doc(doc.RootElement.Clone(), true);
        }
        catch (JsonException)
        {
            return None;
        }
    }

    public static Doc Wrap(JsonElement el) => new(el, true);

    /// <summary>A child by name. Missing yields <see cref="None"/>.</summary>
    public Doc this[string name]
    {
        get
        {
            if (!_ok || _el.ValueKind != JsonValueKind.Object)
            {
                return None;
            }

            return _el.TryGetProperty(name, out JsonElement child)
                ? new Doc(child, true)
                : None;
        }
    }

    public IEnumerable<Doc> Items()
    {
        if (!_ok || _el.ValueKind != JsonValueKind.Array)
        {
            yield break;
        }

        foreach (JsonElement item in _el.EnumerateArray())
        {
            yield return new Doc(item, true);
        }
    }

    public IEnumerable<KeyValuePair<string, Doc>> Fields()
    {
        if (!_ok || _el.ValueKind != JsonValueKind.Object)
        {
            yield break;
        }

        foreach (JsonProperty p in _el.EnumerateObject())
        {
            yield return new KeyValuePair<string, Doc>(p.Name, new Doc(p.Value, true));
        }
    }

    public int Count => _ok && _el.ValueKind == JsonValueKind.Array
        ? _el.GetArrayLength()
        : 0;

    public string Str(string fallback = "")
    {
        if (!_ok)
        {
            return fallback;
        }

        return _el.ValueKind switch
        {
            JsonValueKind.String => _el.GetString() ?? fallback,
            JsonValueKind.Null => fallback,
            JsonValueKind.Undefined => fallback,
            // A number or bool where a string was expected is still something
            // worth showing - "8080" beats an empty label.
            _ => _el.ToString(),
        };
    }

    public bool Bool(bool fallback = false)
        => !_ok
            ? fallback
            : _el.ValueKind switch
            {
                JsonValueKind.True => true,
                JsonValueKind.False => false,
                _ => fallback,
            };

    public double Num(double fallback = 0.0)
        => _ok && _el.ValueKind == JsonValueKind.Number && _el.TryGetDouble(out double d)
            ? d
            : fallback;

    public int Int(int fallback = 0)
    {
        if (!_ok || _el.ValueKind != JsonValueKind.Number)
        {
            return fallback;
        }

        // Python writes ints as ints, but a value that has been through a
        // float - a rounded probability, a computed port - arrives as 8080.0
        // and TryGetInt32 refuses it outright.
        if (_el.TryGetInt32(out int i))
        {
            return i;
        }

        return _el.TryGetDouble(out double d) ? (int)Math.Round(d) : fallback;
    }

    /// <summary>Null when the key is absent or JSON null, rather than a zero.</summary>
    public int? IntOrNull()
        => _ok && _el.ValueKind == JsonValueKind.Number ? Int() : null;

    public double? NumOrNull()
        => _ok && _el.ValueKind == JsonValueKind.Number ? Num() : null;

    /// <summary>The raw JSON text, for round-tripping a value back unchanged.</summary>
    public string Raw() => _ok ? _el.GetRawText() : "null";

    /// <summary>
    /// The value as a settings value would be written by a human: "true",
    /// "0.5", "loopback". Used for display only - never for comparison.
    /// </summary>
    public string Display()
        => Kind switch
        {
            JsonValueKind.True => "true",
            JsonValueKind.False => "false",
            JsonValueKind.Number => Num().ToString("0.####", CultureInfo.InvariantCulture),
            JsonValueKind.String => Str(),
            JsonValueKind.Null => "",
            JsonValueKind.Undefined => "",
            _ => Raw(),
        };
}

using System;
using System.Windows;
using System.Windows.Controls;
using LiveTranscription.Ui.Settings;

namespace LiveTranscription.Ui.Views;

/// <summary>
/// Picks the control template for one settings row, keyed on the field's
/// <c>kind</c> from the schema.
/// </summary>
/// <remarks>
/// <para>
/// Throwing on an unmapped kind is deliberate and load-bearing.
/// <see cref="SettingsVm.KnownKinds"/> already refuses a kind at startup, but
/// that list and this switch can drift apart - someone adds the kind there and
/// forgets the template here - and the failure mode of
/// <see cref="DataTemplateSelector"/> returning null is a control that renders
/// as bare ToString() text: it LOOKS broken rather than IS broken, and nothing
/// says why. An exception here lands in the dispatcher hook and becomes a
/// banner naming the kind.
/// </para>
/// <para>
/// The one key special-cased by name: <c>model</c> is declared in settings.py
/// as a <c>path</c>, because on the command line it is one. In a panel it is a
/// picker over what is actually in _models - <see cref="SettingsVm.SetModels"/>
/// fills its choice list - so it takes the choice template. webui/app.js:381
/// makes the same exception for the same reason.
/// </para>
/// </remarks>
public sealed class FieldTemplateSelector : DataTemplateSelector
{
    public DataTemplate? BoolTemplate { get; set; }
    public DataTemplate? IntTemplate { get; set; }
    public DataTemplate? FloatTemplate { get; set; }
    public DataTemplate? StrTemplate { get; set; }
    public DataTemplate? ChoiceTemplate { get; set; }
    public DataTemplate? PathTemplate { get; set; }
    public DataTemplate? ColorTemplate { get; set; }

    public override DataTemplate? SelectTemplate(object? item, DependencyObject container)
    {
        if (item is not FieldVm field)
        {
            return base.SelectTemplate(item, container);
        }

        DataTemplate? chosen = field.Key == "model"
            ? ChoiceTemplate
            : field.Kind switch
            {
                "bool" => BoolTemplate,
                "int" => IntTemplate,
                "float" => FloatTemplate,
                "str" => StrTemplate,
                "choice" => ChoiceTemplate,
                "path" => PathTemplate,
                "color" => ColorTemplate,
                _ => null,
            };

        return chosen ?? throw new InvalidOperationException(
            "No template for settings field '" + field.Key + "' (kind '"
            + field.Kind + "'). Add a DataTemplate to Views/FieldTemplates.xaml, "
            + "a property here, and the kind to SettingsVm.KnownKinds.");
    }
}

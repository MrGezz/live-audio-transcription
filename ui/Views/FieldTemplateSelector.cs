using System.Windows;
using System.Windows.Controls;

namespace LiveTranscription.Ui.Selectors
{
    public class FieldTemplateSelector : DataTemplateSelector
    {
        public DataTemplate BoolTemplate { get; set; }
        public DataTemplate IntTemplate { get; set; }
        public DataTemplate StringTemplate { get; set; }
        // Add DataTemplate properties for float, choice, path, color...

        public override DataTemplate SelectTemplate(object item, DependencyObject container)
        {
            if (item is FieldViewModel field)
            {
                // Must map every kind in the live schema so an unmapped kind fails loudly
                return field.Kind switch
                {
                    "bool" => BoolTemplate,
                    "int" => IntTemplate,
                    "str" => StringTemplate,
                    // ... map other types
                    _ => throw new System.ArgumentException($"Unknown field kind: {field.Kind}")
                };
            }
            return base.SelectTemplate(item, container);
        }
    }
}